"""Валидация и операции с журналом. Денежные значения — целые сотые"""

import re
import sqlite3
import calendar

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from collections.abc import Iterable, Mapping
from typing import Literal, TypedDict, cast

from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import NotFound

from . import db

CURRENCIES: tuple[str, ...] = ('RUB', 'USD', 'EUR')
SYMBOLS: dict[str, str] = {'RUB': '₽', 'USD': '$', 'EUR': '€'}

EntryKind = Literal['income', 'expense', 'transfer']

class EntryTag(TypedDict):
	entry_id: int
	id: int
	name: str

class Entry(TypedDict):
	id: int
	profile_id: int
	kind: EntryKind
	currency: str
	amount: int
	destination_currency: str | None
	destination_amount: int | None
	occurred_at: str
	comment: str
	category_id: int | None
	category: str
	tags: list[EntryTag]
	planned: bool

class Breakdown(TypedDict):
	name: str
	income: int
	expense: int

class StatisticsGroup(TypedDict):
	totals: dict[str, int]
	count: int
	entries: list[Entry]
	categories: list[Breakdown]
	tags: list[Breakdown]
	transfers: dict[str, int]

class Statistics(TypedDict):
	current: StatisticsGroup
	planned: StatisticsGroup
	start_date: str
	end_date: str

class ValidationError(ValueError):
	pass

def to_money(raw: str | int | float | Decimal, *, capital: bool = False) -> int:
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
	value = raw.strip()
	if not 1 <= len(value) <= 80:
		raise ValidationError('Название должно содержать от 1 до 80 символов')

	return value

def fetch_owned(table: str, item_id: int | str | None, profile_id: int) -> sqlite3.Row:
	if table not in ('entries', 'categories', 'tags'):
		raise ValueError('Unsupported table')

	row = db.get().execute(
		f'SELECT * FROM {table} WHERE id = ? AND profile_id = ?', (item_id, profile_id),
	).fetchone()
	if row is None:
		raise NotFound()

	return cast(sqlite3.Row, row)

def _to_amounts(capitals: Mapping[str, str]) -> dict[str, int]:
	return {
		c: to_money(capitals.get(c, '0'), capital=True) for c in CURRENCIES
	}

def effective_rate(amount: int, destination_amount: int) -> str:
	"""Вычисленный курс для показа; исходные суммы остаются точными."""
	with localcontext() as context:
		context.prec = 32
		return format(Decimal(destination_amount) / Decimal(abs(amount)), '.8g')

def create_profile(raw_name: str, capitals: Mapping[str, str]) -> int:
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
	fetch_owned('categories', category_id, profile_id)
	with db.get() as dbo:
		dbo.execute('UPDATE entries SET category_id=NULL WHERE category_id=? AND profile_id=?', (category_id, profile_id))
		dbo.execute('DELETE FROM categories WHERE id=? AND profile_id=?', (category_id, profile_id))

def fetch_entries_for(profile_id: int, now: datetime | None = None) -> list[Entry]:
	current_time = (now or datetime.now()).isoformat(timespec='seconds')
	dbo = db.get()
	entries = cast(list[Entry], [dict(r) for r in dbo.execute(
		"SELECT e.*, COALESCE(c.name, 'Без категории') AS category "
		'FROM entries e '
		'LEFT JOIN categories c ON c.id = e.category_id AND c.profile_id = e.profile_id '
		'WHERE e.profile_id = ? '
		'ORDER BY e.occurred_at DESC, e.id DESC',
		(profile_id,)
	)])

	tags: dict[int, list[EntryTag]] = {}
	for row in dbo.execute(
		'SELECT et.entry_id, t.id, t.name FROM entry_tags et '
		'JOIN tags t ON t.id = et.tag_id AND t.profile_id = et.profile_id '
		'WHERE et.profile_id = ? ORDER BY t.name',
		(profile_id,)
	):
		tags.setdefault(row['entry_id'], []).append(cast(EntryTag, dict(row)))
	
	for entry in entries:
		entry['tags'] = tags.get(entry['id'], [])
		entry['kind'] = 'transfer' if entry['destination_currency'] is not None else 'income' if entry['amount'] > 0 else 'expense'
		entry['planned'] = entry['occurred_at'] > current_time
	return entries

def fetch_balances(
	profile_id: int,
	entries: Iterable[Entry],
	now: datetime | None = None
) -> dict[str, int]:
	current_time = (now or datetime.now()).isoformat(timespec='seconds')
	result: dict[str, int] = {
		r['currency']: r['amount'] for r in db.get().execute(
			'SELECT * FROM capitals WHERE profile_id = ?', (profile_id,)
		)
	}

	for entry in entries:
		if entry['occurred_at'] > current_time:
			continue
		result[entry['currency']] += entry['amount']
		if entry['destination_currency'] is not None:
			assert entry['destination_amount'] is not None
			result[entry['destination_currency']] += entry['destination_amount']
	return result

def _summarize(entries: list[Entry], currency: str) -> StatisticsGroup:
	totals = {
		k: sum(abs(e['amount']) for e in entries if e['kind'] == k) for k in ('income', 'expense')
	}
	transfers = {'sent': 0, 'received': 0, 'count': 0}
	groups: dict[str, dict[str, Breakdown]] = {'categories': {}, 'tags': {}}
	
	for e in entries:
		if e['kind'] == 'transfer':
			transfers['count'] += 1
			if e['currency'] == currency:
				transfers['sent'] += abs(e['amount'])
			else:
				assert e['destination_amount'] is not None
				transfers['received'] += e['destination_amount']
			continue
		for group, labels in (
			('categories', [e['category']]),
			('tags', [t['name'] for t in e['tags']])
		):
			for label in labels:
				bucket = groups[group].setdefault(
					label, {'name': label, 'income': 0, 'expense': 0}
				)
				bucket[e['kind']] += abs(e['amount'])

	return {
		'totals': totals,
		'count': len(entries),
		'entries': entries,
		'transfers': transfers,
		'categories': sorted(
			groups['categories'].values(),
			key=lambda r: r['income'] + r['expense'], reverse=True
		),
		'tags': sorted(
			groups['tags'].values(), key=lambda r: r['income'] + r['expense'], reverse=True
		),
	}

def calc_statistics(
	entries: Iterable[Entry],
	period: str,
	selected: str,
	currency: str,
	end_selected: str | None = None, now: datetime | None = None,
) -> Statistics:
	start, end = period_bounds(period, selected, end_selected)
	current_time = (now or datetime.now()).isoformat(timespec='seconds')
	matched = [
		e for e in entries if start.isoformat() <= e['occurred_at'][:10] <= end.isoformat()
		and (e['currency'] == currency or e['destination_currency'] == currency)
	]

	return {
		'current': _summarize([e for e in matched if e['occurred_at'] <= current_time], currency),
		'planned': _summarize([e for e in matched if e['occurred_at'] > current_time], currency),
		'start_date': start.isoformat(), 'end_date': end.isoformat(),
	}

def period_bounds(
	period: str, selected: str, end_selected: str | None = None
) -> tuple[date, date]:
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
