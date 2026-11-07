"""Правила учёта: целые сотые, знаковые суммы и вычисляемое планирование."""

import re
import sqlite3
import calendar

from datetime import date, datetime, time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from collections.abc import Iterable, Mapping
from typing import Literal, TypeAlias, cast

from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import NotFound

from . import db

# Коды валют в порядке отображения; все суммы каждой валюты считаются отдельно.
CURRENCIES: tuple[str, ...] = ('RUB', 'USD', 'EUR')
# Символы валют используются только для отображения в шаблонах.
SYMBOLS: dict[str, str] = {'RUB': '₽', 'USD': '$', 'EUR': '€'}

# Категория по умолчанию
DEFAULT_CATEGORY_NAME = 'Без категории'

# Вычисляемый UI-тип; в таблице entries такого столбца нет.
EntryKind: TypeAlias = Literal['income', 'expense', 'transfer']

@dataclass(frozen=True, slots=True)
class EntryTag:
	"""Тег записи; идентификатор родительской записи здесь не дублируется."""

	id: int
	name: str

@dataclass(slots=True)
class Entry:
	"""Запись журнала с точной суммой и единожды разобранным временем.

	Справочное имя категории загружается JOIN, теги — отдельным общим запросом.
	Тип и плановое состояние не сохраняются в объекте.
	"""

	id: int
	currency: str
	amount: int
	occurred_at: datetime
	comment: str
	category_id: int | None
	category: str
	destination_currency: str | None = None
	destination_amount: int | None = None
	tags: list[EntryTag] = field(default_factory=list)

	@property
	def kind(self) -> EntryKind:
		"""Вычислить UI-тип за O(1), без обращения к БД."""
		if self.destination_currency is not None:
			return 'transfer'
		return 'income' if self.amount > 0 else 'expense'

	def is_planned(self, now: datetime) -> bool:
		"""Сравнить с единым моментом запроса; равенство означает текущую запись."""
		return self.occurred_at > now

	@classmethod
	def from_row(
		cls,
		row: sqlite3.Row, *,
		category: str = DEFAULT_CATEGORY_NAME,
		tags: list[EntryTag] | None = None,
	) -> 'Entry':
		"""Преобразовать строку SQLite без дополнительных запросов и profile_id."""
		return cls(
			id=row['id'],
			currency=row['currency'],
			amount=row['amount'],
			occurred_at=datetime.fromisoformat(row['occurred_at']),
			comment=row['comment'],
			category_id=row['category_id'],
			category=category,
			destination_currency=row['destination_currency'],
			destination_amount=row['destination_amount'],
			tags=tags if tags is not None else [],
		)

@dataclass(slots=True)
class Totals:
	"""Уже рассчитанные положительные суммы доходов и расходов в сотых."""

	income: int = 0
	expense: int = 0

@dataclass(slots=True)
class Breakdown:
	"""Положительные суммы одной категории или тега в выбранной валюте."""

	name: str
	income: int = 0
	expense: int = 0

@dataclass(slots=True)
class TransferTotals:
	"""Суммы и число трансферов; восстановление count потребовало бы обхода."""

	sent: int = 0
	received: int = 0
	count: int = 0

@dataclass(slots=True)
class StatisticsGroup:
	"""Группа записей с агрегатами, вычисленными за один проход."""

	entries: list[Entry]
	totals: Totals = field(default_factory=Totals)
	transfers: TransferTotals = field(default_factory=TransferTotals)
	categories: list[Breakdown] = field(default_factory=list)
	tags: list[Breakdown] = field(default_factory=list)

	@property
	def count(self) -> int:
		"""Число записей за O(1), без отдельного сохранённого счётчика."""
		return len(self.entries)

@dataclass(slots=True)
class Statistics:
	"""Независимые текущая/плановая группы и включительные даты периода."""

	current: StatisticsGroup
	planned: StatisticsGroup
	start_date: date
	end_date: date

class ValidationError(ValueError):
	"""Ошибка пользовательского ввода, которую можно показать в форме."""
	pass

# ====================

def to_money(raw: str | int | float | Decimal, *, capital: bool = False) -> int:
	"""Разобрать сумму в целые копейки/центы без округления.
	
	Допускаются запятая, точка и пробелы-разделители; максимум два знака.
	Для капитала разрешены ноль и отрицательные значения, для операций –
	только положительные. При неверном формате или размере – ValidationError."""
	value = str(raw).strip().replace(' ', '').replace('\u00a0', '').replace(',', '.')
	pattern: str = r'-?\d+(?:\.\d{1,2})?' if capital else r'\d+(?:\.\d{1,2})?'
	if not re.fullmatch(pattern, value):
		raise ValidationError('Введите сумму числом с точностью до двух знаков после запятой')
	try:
		result = int(Decimal(value) * 100)

	except (InvalidOperation, ValueError, OverflowError):
		raise ValidationError('Некорректная сумма') from None
	if abs(result) > 900_000_000_000_000_000:
		raise ValidationError('Слишком большая сумма')
	elif not capital and result <= 0:
		raise ValidationError('Сумма операции должна быть больше нуля')
	return result

def to_name(raw: str) -> str:
	"""Обрезать крайние пробелы и проверить длину названия от 1 до 80 символов."""
	value = raw.strip()
	if not 1 <= len(value) <= 80:
		raise ValidationError('Название должно содержать от 1 до 80 символов')

	return value

def fetch_owned(table: str, item_id: int | str | None, profile_id: int) -> sqlite3.Row:
	"""Найти запись разрешённой таблицы внутри профиля или поднять NotFound.
	
	Имя таблицы проверяется по списку, значения передаются параметрами SQL."""
	if table not in ('entries', 'categories', 'tags'):
		raise ValueError('Unsupported table')

	row = db.get().execute(
		f'SELECT * FROM {table} WHERE id = ? AND profile_id = ?', (item_id, profile_id),
	).fetchone()
	if row is None:
		raise NotFound()

	return cast(sqlite3.Row, row)

def _to_amounts(capitals: Mapping[str, str]) -> dict[str, int]:
	"""Преобразовать начальный капитал каждой валюты в сотые; отсутствующий – ноль."""
	return { c: to_money(capitals.get(c, '0'), capital=True) for c in CURRENCIES }

def effective_rate(amount: int, destination_amount: int) -> str:
	"""Вычислить курс получения к модулю списания для показа с 8 значащими цифрами.
	
	Исходные суммы должны быть ненулевыми сотыми; результат не хранится в БД."""
	with localcontext() as context:
		context.prec = 32
		return format(Decimal(destination_amount) / Decimal(abs(amount)), '.8g')

def create_profile(raw_name: str, capitals: Mapping[str, str]) -> int:
	"""Создать профиль и начальный капитал трёх валют одной транзакцией; вернуть ID."""
	title, amounts = to_name(raw_name), _to_amounts(capitals)

	with db.get() as dbo:
		profile_id = dbo.execute(
			'INSERT INTO profiles(name) VALUES (?)', (title,)
		).lastrowid

		assert profile_id is not None  # INSERT into an INTEGER PRIMARY KEY produces a row id.
		dbo.executemany(
			'INSERT INTO capitals VALUES (?, ?, ?)',
			[(profile_id, c, amounts[c]) for c in CURRENCIES]
		)
	return profile_id

def save_profile(profile_id: int, raw_name: str, capitals: Mapping[str, str]) -> None:
	"""Атомарно изменить название и капитал профиля без создания операций журнала."""
	title, amounts = to_name(raw_name), _to_amounts(capitals)

	with db.get() as dbo:
		dbo.execute('UPDATE profiles SET name = ? WHERE id = ?', (title, profile_id))
		dbo.executemany(
			'UPDATE capitals SET amount = ? WHERE profile_id = ? AND currency = ?',
			[(amounts[c], profile_id, c) for c in CURRENCIES]
		)

def save_entry(
	profile_id: int,
	form: MultiDict[str, str],
	entry_id: int | None = None,
) -> int:
	"""Проверить форму и атомарно создать либо изменить запись; вернуть её ID.
	
	Форма передаёт положительный amount и UI-тип kind; в БД сохраняется знак.
	Для трансфера обязательны другая валюта и destination_amount; курс игнорируется.
	Будущие даты разрешены. Категория и теги проверяются на принадлежность профилю.
	При редактировании связи тегов полностью заменяются; ошибки – ValidationError."""
	if entry_id is not None:
		fetch_owned('entries', entry_id, profile_id)

	amount = to_money(form.get('amount', ''))
	kind, currency = form.get('kind'), form.get('currency')
	if kind not in ('income', 'expense', 'transfer') or currency not in CURRENCIES:
		raise ValidationError('Выберите тип операции и валюту')

	destination_currency: str | None = None
	destination_amount: int | None = None
	if kind == 'transfer':
		destination_currency = form.get('destination_currency')
		if destination_currency not in CURRENCIES or destination_currency == currency:
			raise ValidationError('Для трансфера выберите две разные валюты')
		destination_amount = to_money(form.get('destination_amount', ''))

	comment, raw_date = form.get('comment', '').strip(), form.get('occurred_at', '')
	if not 1 <= len(comment) <= 2000:
		raise ValidationError('Комментарий обязателен, не более 2000 символов')
	elif not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?', raw_date):
		raise ValidationError('Укажите корректные дату и время')

	try:
		occurred_at = datetime.fromisoformat(raw_date)
	except ValueError:
		raise ValidationError('Укажите корректные дату и время') from None

	dbo = db.get()
	category_id: str | None = form.get('category_id') or None
	if category_id is not None:
		fetch_owned('categories', category_id, profile_id)

	tags = set(form.getlist('tags'))
	for tag in tags:
		fetch_owned('tags', tag, profile_id)

	values = (
		currency, amount if kind == 'income' else -amount, occurred_at.isoformat(timespec='seconds'),
		comment, category_id, destination_currency, destination_amount,
	)

	with dbo:
		if entry_id is None:
			entry_id = dbo.execute(
				'INSERT INTO entries'
				'(currency, amount, occurred_at, comment, category_id, '
				'destination_currency, destination_amount, profile_id) '
				'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
				(*values, profile_id),
			).lastrowid
			assert entry_id is not None  # The INSERT always creates a row id.
		else:
			dbo.execute(
				'UPDATE entries SET currency = ?, amount = ?, '
				'occurred_at = ?, comment = ?, category_id = ?, '
				'destination_currency = ?, destination_amount = ? '
				'WHERE id = ? AND profile_id = ?',
				(*values, entry_id, profile_id)
			)
			dbo.execute(
				'DELETE FROM entry_tags WHERE entry_id=? AND profile_id = ?',
				(entry_id, profile_id)
			)

		dbo.executemany(
			'INSERT INTO entry_tags VALUES (?, ?, ?)',
			[(entry_id, tag, profile_id) for tag in tags]
		)
	return entry_id

def delete_category(category_id: int, profile_id: int) -> None:
	"""Удалить категорию профиля, предварительно установив её записям NULL.
	
	Обе операции выполняются одной транзакцией. Составной внешний ключ
	не использует ON DELETE SET NULL, чтобы не обнулять обязательный profile_id."""
	fetch_owned('categories', category_id, profile_id)
	with db.get() as dbo:
		dbo.execute(
			'UPDATE entries SET category_id = NULL '
			'WHERE category_id = ? AND profile_id = ?',
			(category_id, profile_id)
		)
		dbo.execute(
			'DELETE FROM categories WHERE id = ? AND profile_id = ?',
			(category_id, profile_id)
		)

def fetch_entries_for(profile_id: int) -> list[Entry]:
	"""Загрузить журнал и теги двумя запросами независимо от числа записей."""
	connection = db.get()
	rows = connection.execute(
		"SELECT e.id, e.currency, e.amount, e.occurred_at, e.comment, e.category_id, "
		"COALESCE(c.name, 'Без категории'), e.destination_currency, e.destination_amount "
		'FROM entries e '
		'LEFT JOIN categories c ON c.id = e.category_id AND c.profile_id = e.profile_id '
		'WHERE e.profile_id = ? ORDER BY e.occurred_at DESC, e.id DESC',
		(profile_id,),
	).fetchall()
	tags: dict[int, list[EntryTag]] = {}

	for row in connection.execute(
		'SELECT et.entry_id, t.id, t.name FROM entry_tags et '
		'JOIN tags t ON t.id=et.tag_id AND t.profile_id=et.profile_id '
		'WHERE et.profile_id=? ORDER BY t.name', (profile_id,),
	):
		tags.setdefault(row['entry_id'], []).append(EntryTag(row['id'], row['name']))

	# Порядок столбцов задан SELECT явно: индексный доступ к Row не ищет имена.
	return [Entry(
		id=row[0],
		currency=row[1],
		amount=row[2],
		occurred_at=datetime.fromisoformat(row[3]),
		comment=row[4],
		category_id=row[5],
		category=row[6],
		destination_currency=row[7],
		destination_amount=row[8],
		tags=tags.get(row[0], []),
	) for row in rows]

def fetch_balances(
	profile_id: int, entries: Iterable[Entry], now: datetime | None = None,
) -> dict[str, int]:
	"""Прибавить наступившие движения к капиталу; будущий трансфер исключается целиком."""
	current_time = now or datetime.now()
	result: dict[str, int] = {
		r['currency']: r['amount'] for r in db.get().execute(
			'SELECT currency, amount FROM capitals WHERE profile_id = ?', (profile_id,),
		)
	}

	for entry in entries:
		if entry.is_planned(current_time):
			continue

		result[entry.currency] += entry.amount
		if entry.destination_currency is not None:
			assert entry.destination_amount is not None
			result[entry.destination_currency] += entry.destination_amount

	return result

def _summarize(entries: list[Entry], currency: str) -> StatisticsGroup:
	"""За один проход рассчитать группу; одна запись может войти в несколько тегов."""
	result = StatisticsGroup(entries)
	categories: dict[str, Breakdown] = {}
	tags: dict[str, Breakdown] = {}

	for entry in entries:
		amount = entry.amount
		if entry.destination_currency is not None:
			result.transfers.count += 1
			if entry.currency == currency:
				result.transfers.sent -= amount
			else:
				assert entry.destination_amount is not None
				result.transfers.received += entry.destination_amount
			continue

		if amount > 0:
			result.totals.income += amount
		else:
			result.totals.expense -= amount

		for group, labels in (
			(categories, [entry.category]),
			(tags, [tag.name for tag in entry.tags])
		):
			for label in labels:
				bucket = group.get(label)
				if bucket is None:
					bucket = group[label] = Breakdown(label)
				if amount > 0:
					bucket.income += amount
				else:
					bucket.expense -= amount

	result.categories = sorted(
		categories.values(), key=lambda row: row.income + row.expense, reverse=True
	)
	result.tags = sorted(
		tags.values(), key=lambda row: row.income + row.expense, reverse=True
	)
	return result

def calc_statistics(
	entries: Iterable[Entry],
	period: str,
	selected: str,
	currency: str,
	end_selected: str | None = None,
	now: datetime | None = None,
) -> Statistics:
	"""Одним проходом отобрать валюту и включительный период, разделив группы по now."""
	start, end = period_bounds(period, selected, end_selected)
	start_time = datetime.combine(start, time.min)
	end_time = datetime.combine(end, time.max)
	current_time = now or datetime.now()
	current: list[Entry] = []
	planned: list[Entry] = []

	for entry in entries:
		if (not start_time <= entry.occurred_at <= end_time
			or currency not in (entry.currency, entry.destination_currency)):
			continue

		(planned if entry.is_planned(current_time) else current).append(entry)

	return Statistics(
		_summarize(current, currency),
		_summarize(planned, currency),
		start,
		end
	)

def period_bounds(
	period: str, selected: str, end_selected: str | None = None
) -> tuple[date, date]:
	"""Вернуть включительные даты периода или поднять ValidationError.
	
	selected: YYYY-MM-DD для дня, недели и диапазона, YYYY-MM для месяца,
	YYYY для года. Неделя – последние 7 дней до selected включительно.
	Для range дополнительно нужен end_selected в формате YYYY-MM-DD."""

	try:
		if period in ('day', 'week', 'range'):
			if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', selected):
				raise ValueError()

			start = end = date.fromisoformat(selected)
			if period == 'week':
				start = date.fromordinal(max(1, end.toordinal() - 6))
			elif period == 'range':
				if not end_selected or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', end_selected):
					raise ValueError()

				end = date.fromisoformat(end_selected)
		elif period == 'month':
			start = datetime.strptime(selected, '%Y-%m').date().replace(day=1)
			end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
		elif period == 'year' and re.fullmatch(r'\d{4}', selected) and 1 <= int(selected) <= 9999:
			start, end = date(int(selected), 1, 1), date(int(selected), 12, 31)
		else:
			raise ValueError()
	except ValueError:
		raise ValidationError('Укажите корректный период статистики') from None
	
	if start > end:
		raise ValidationError('Начальная дата периода не должна быть позже конечной')
	return start, end
