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

# Отображаемое имя отсутствующей категории; отдельной строки в БД нет.
DEFAULT_CATEGORY_NAME: str = 'Без категории'

# Вычисляемый UI-тип; в таблице entries такого столбца нет.
EntryKind: TypeAlias = Literal['income', 'expense', 'transfer']

@dataclass(frozen=True, slots=True)
class EntryTag:
	"""Идентификатор и название тега, прикреплённого к записи."""

	id: int
	name: str

@dataclass(slots=True)
class Entry:
	"""Запись: знаковые сотые, локальное время сервера, категория и теги.

	amount положителен для дохода; трансфер дополнительно содержит положительную
	сумму destination_amount в destination_currency. category — имя из JOIN,
	category_id=None означает «Без категории». Тип и планирование вычисляются."""

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
		"""Определить тип по знаку суммы и наличию получаемой валюты.

		Returns:
		    income, expense или transfer; обращения к БД нет."""
		if self.destination_currency is not None:
			return 'transfer'
		return 'income' if self.amount > 0 else 'expense'

	def is_planned(self, now: datetime) -> bool:
		"""Проверить, наступило ли время записи.

		Args:
		    now: Локальное время сервера без часового смещения.
		Returns:
		    True для будущей записи; равенство означает текущую запись.
		Raises:
		    TypeError: Смешаны даты с часовым смещением и без него."""
		return self.occurred_at > now

	@classmethod
	def from_row(
		cls,
		row: sqlite3.Row, *,
		category: str = DEFAULT_CATEGORY_NAME,
		tags: list[EntryTag] | None = None,
	) -> 'Entry':
		"""Преобразовать строку SQLite в запись без дополнительных запросов.

		Args:
		    row: Строка entries с суммами в сотых и occurred_at в ISO-формате.
		    category: Отображаемое название категории.
		    tags: Теги записи; переданный список переиспользуется.
		Returns:
		    Запись с разобранным datetime; profile_id не копируется.
		Raises:
		    IndexError: В строке нет нужного столбца.
		    ValueError: В БД хранится некорректная дата."""
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
	"""Положительные итоги доходов и расходов в сотых выбранной валюты."""

	income: int = 0
	expense: int = 0

@dataclass(slots=True)
class Breakdown:
	"""Название категории/тега и его положительные итоги в сотых."""

	name: str
	income: int = 0
	expense: int = 0

@dataclass(slots=True)
class TransferTotals:
	"""Списанные и полученные сотые выбранной валюты и число трансферов."""

	sent: int = 0
	received: int = 0
	count: int = 0

@dataclass(slots=True)
class StatisticsGroup:
	"""Записи одной временной группы и рассчитанные за один проход итоги."""

	entries: list[Entry]
	totals: Totals = field(default_factory=Totals)
	transfers: TransferTotals = field(default_factory=TransferTotals)
	categories: list[Breakdown] = field(default_factory=list)
	tags: list[Breakdown] = field(default_factory=list)

	@property
	def count(self) -> int:
		"""Получить число записей без отдельного счётчика.

		Returns:
		    Длина списка entries за O(1)."""
		return len(self.entries)

@dataclass(slots=True)
class Statistics:
	"""Текущая и плановая статистика с включительными границами периода."""

	current: StatisticsGroup
	planned: StatisticsGroup
	start_date: date
	end_date: date

class ValidationError(ValueError):
	"""Ошибка пользовательского ввода, которую можно показать в форме."""
	pass

# ====================

def to_money(raw: str | int | float | Decimal, *, capital: bool = False) -> int:
	"""Разобрать денежную сумму без округления.

	Args:
	    raw: Число или строка; допустимы точка, запятая и пробелы-разделители.
	    capital: Разрешить ноль и отрицательное значение начального капитала.
	Returns:
	    Целые копейки/центы; для операций строго больше нуля.
	Raises:
	    ValidationError: Неверный формат, знак, больше двух дробных знаков или лимита."""
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
	"""Проверить название после удаления крайних пробелов.

	Args:
	    raw: Введённое пользователем название.
	Returns:
	    Название длиной от 1 до 80 символов.
	Raises:
	    ValidationError: Пустое или слишком длинное название."""
	value = raw.strip()
	if not 1 <= len(value) <= 80:
		raise ValidationError('Название должно содержать от 1 до 80 символов')

	return value

def fetch_owned(table: str, item_id: int | str | None, profile_id: int) -> sqlite3.Row:
	"""Найти объект разрешённой таблицы внутри заданного профиля.

	Args:
	    table: entries, categories или tags.
	    item_id: Идентификатор объекта; None не находит строку.
	    profile_id: Владелец объекта.
	Returns:
	    Строка SQLite со всеми столбцами объекта.
	Raises:
	    ValueError: Таблица не разрешена.
	    NotFound: Объект отсутствует или принадлежит другому профилю.
	    sqlite3.Error: Не удалось прочитать БД."""
	if table not in ('entries', 'categories', 'tags'):
		raise ValueError('Unsupported table')

	row = db.get().execute(
		f'SELECT * FROM {table} WHERE id = ? AND profile_id = ?', (item_id, profile_id),
	).fetchone()
	if row is None:
		raise NotFound()

	return cast(sqlite3.Row, row)

def _to_amounts(capitals: Mapping[str, str]) -> dict[str, int]:
	"""Проверить начальный капитал всех поддерживаемых валют.

	Args:
	    capitals: Суммы по кодам валют; отсутствующая сумма равна нулю.
	Returns:
	    Суммы RUB/USD/EUR в целых сотых.
	Raises:
	    ValidationError: Одна из сумм недопустима."""
	return { c: to_money(capitals.get(c, '0'), capital=True) for c in CURRENCIES }

def effective_rate(amount: int, destination_amount: int) -> str:
	"""Рассчитать курс получения к списанию с восемью значащими цифрами.

	Args:
	    amount: Ненулевая исходная сумма в сотых; используется модуль.
	    destination_amount: Положительная полученная сумма в сотых.
	Returns:
	    Десятичная строка курса для отображения.
	Raises:
	    decimal.DivisionByZero: Исходная сумма равна нулю при ненулевом получении.
	    decimal.InvalidOperation: Обе суммы равны нулю."""
	with localcontext() as context:
		context.prec = 32
		return format(Decimal(destination_amount) / Decimal(abs(amount)), '.8g')

def create_profile(raw_name: str, capitals: Mapping[str, str]) -> int:
	"""Создать профиль и начальный капитал одной транзакцией.

	Args:
	    raw_name: Название профиля до проверки.
	    capitals: Начальные суммы по кодам валют; отсутствующие равны нулю.
	Returns:
	    Идентификатор созданного профиля.
	Raises:
	    ValidationError: Название или сумма недопустимы.
	    sqlite3.Error: Не удалось сохранить профиль и капитал."""
	title, amounts = to_name(raw_name), _to_amounts(capitals)

	with db.get() as dbo:
		profile_id = dbo.execute(
			'INSERT INTO profiles(name) VALUES (?)', (title,)
		).lastrowid

		assert profile_id is not None  # INTEGER PRIMARY KEY получает ID при INSERT.
		dbo.executemany(
			'INSERT INTO capitals VALUES (?, ?, ?)',
			[(profile_id, c, amounts[c]) for c in CURRENCIES]
		)
	return profile_id

def save_profile(profile_id: int, raw_name: str, capitals: Mapping[str, str]) -> None:
	"""Атомарно изменить название и начальный капитал без записи в журнале.

	Args:
	    profile_id: Идентификатор существующего профиля.
	    raw_name: Новое название до проверки.
	    capitals: Новые суммы по кодам валют; отсутствующие равны нулю.
	Raises:
	    ValidationError: Название или сумма недопустимы.
	    sqlite3.Error: Не удалось сохранить изменения."""
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
	"""Проверить и сохранить операцию вместе со связями тегов одной транзакцией.

	Args:
	    profile_id: Владелец операции, категории и тегов.
	    form: Поля формы: kind, положительный amount, currency, occurred_at,
	        comment; необязательные category_id и повторяющиеся tags.
	        Трансфер требует destination_currency и destination_amount; курс игнорируется.
	    entry_id: Редактируемая запись; None создаёт новую. Будущие даты разрешены.
	Returns:
	    Идентификатор сохранённой записи; при редактировании теги заменяются.
	Raises:
	    ValidationError: Недопустимые поля операции.
	    NotFound: Запись, категория или тег отсутствует в профиле.
	    sqlite3.Error: Не удалось сохранить операцию или её теги."""
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
			assert entry_id is not None  # INSERT создал запись с INTEGER PRIMARY KEY.
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
	"""Удалить категорию, переведя её операции в «Без категории» атомарно.

	Args:
	    category_id: Идентификатор удаляемой категории.
	    profile_id: Владелец категории и записей.
	Raises:
	    NotFound: Категория отсутствует в профиле.
	    sqlite3.Error: Не удалось обновить записи или удалить категорию."""
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
	"""Загрузить журнал и теги двумя запросами независимо от числа записей.

	Args:
	    profile_id: Профиль журнала.
	Returns:
	    Записи от новых к старым; при равном времени — по убыванию ID.
	Raises:
	    sqlite3.Error: Не удалось прочитать журнал или теги.
	    ValueError: В БД хранится некорректная дата."""
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
	"""Прибавить наступившие движения к начальному капиталу профиля.

	Args:
	    profile_id: Профиль для загрузки начального капитала.
	    entries: Записи этого же профиля; будущие трансферы исключаются целиком.
	    now: Момент расчёта; None использует текущее локальное время сервера.
	Returns:
	    Баланс каждой валюты в целых сотых.
	Raises:
	    sqlite3.Error: Не удалось прочитать начальный капитал.
	    TypeError: Время записей и now имеют несовместимые часовые смещения."""
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
	"""Суммировать записи за один проход; итоги тегов могут пересекаться.

	Args:
	    entries: Уже отобранные записи одной группы и валюты.
	    currency: Валюта расчёта; трансфер содержит её на одной из сторон.
	Returns:
	    Итоги операций, трансферов, категорий и тегов в целых сотых."""
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
	"""Отобрать период и валюту, разделив записи на текущие и плановые.

	Args:
	    entries: Исходный журнал.
	    period: day, week, month, year или range.
	    selected: Выбранная дата/месяц/год в формате period_bounds().
	    currency: Валюта расчёта, учитываемая на обеих сторонах трансфера.
	    end_selected: Включительная конечная дата для range.
	    now: Граница текущих записей; None использует время сервера.
	Returns:
	    Итоги двух временных групп и включительные границы периода.
	Raises:
	    ValidationError: Неверный период или даты.
	    TypeError: Время записей и now имеют несовместимые часовые смещения."""
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
	"""Определить включительные календарные границы периода.

	Args:
	    period: day, week, month, year или range; week — последние семь дней.
	    selected: YYYY-MM-DD для дня/недели/диапазона, YYYY-MM для месяца,
	        YYYY для года. Для недели это последняя дата.
	    end_selected: YYYY-MM-DD конца диапазона; нужен только для range.
	Returns:
	    Начальная и конечная даты, обе включены.
	Raises:
	    ValidationError: Неизвестный период, неверные даты или обратный диапазон."""

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
