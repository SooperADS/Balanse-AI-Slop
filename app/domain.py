"""Валидация и операции с журналом. Денежные значения — целые сотые"""

import re
import sqlite3

from datetime import datetime
from decimal import Decimal, InvalidOperation
from collections.abc import Iterable, Mapping
from typing import Literal, TypedDict, cast

from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import NotFound

from . import db

CURRENCIES: tuple[str, ...] = ('RUB', 'USD', 'EUR')
SYMBOLS: dict[str, str] = {'RUB': '₽', 'USD': '$', 'EUR': '€'}

EntryKind = Literal['income', 'expense']

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
	occurred_at: str
	comment: str
	category_id: int
	category: str
	tags: list[EntryTag]

class Breakdown(TypedDict):
	name: str
	income: int
	expense: int

class Statistics(TypedDict):
	totals: dict[str, int]
	count: int
	entries: list[Entry]
	categories: list[Breakdown]
	tags: list[Breakdown]

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

def create_profile(raw_name: str, capitals: Mapping[str, str]) -> int:
	title, amounts = to_name(raw_name), _to_amounts(capitals)

	with db.get() as dbo:
		profile_id = dbo.execute(
			'INSERT INTO profiles(name) VALUES (?)', (title,)
		).lastrowid

		assert profile_id is not None  # INSERT into an INTEGER PRIMARY KEY produces a row id.

		dbo.execute(
			"INSERT INTO categories(profile_id, name, is_default) "
			"VALUES (?, 'Без категории', 1)",
			(profile_id,)
		)
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
	now: datetime | None = None,
) -> int:
	if entry_id is not None:
		fetch_owned('entries', entry_id, profile_id)

	amount = to_money(form.get('amount', ''))
	kind, currency = form.get('kind'), form.get('currency')
	if kind not in ('income', 'expense') or currency not in CURRENCIES:
		raise ValidationError('Выберите тип операции и валюту')

	comment, raw_date = form.get('comment', '').strip(), form.get('occurred_at', '')
	if not 1 <= len(comment) <= 2000:
		raise ValidationError('Комментарий обязателен, не более 2000 символов')
	elif not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?', raw_date):
		raise ValidationError('Укажите корректные дату и время')

	try:
		occurred_at = datetime.fromisoformat(raw_date)
	except ValueError:
		raise ValidationError('Укажите корректные дату и время') from None

	if occurred_at > (now or datetime.now()):
		raise ValidationError('Нельзя добавлять операции с будущей датой или временем')

	dbo = db.get()
	category_id: str | int | None = form.get('category_id')
	if not category_id:
		category_id = dbo.execute(
			'SELECT id FROM categories WHERE profile_id = ? AND is_default = 1',
			(profile_id,)
		).fetchone()['id']

	fetch_owned('categories', category_id, profile_id)
	tags = set(form.getlist('tags'))
	for tag in tags:
		fetch_owned('tags', tag, profile_id)

	values = (
		kind, currency, amount, occurred_at.isoformat(timespec='seconds'), comment, category_id
	)

	with dbo:
		if entry_id is None:
			entry_id = dbo.execute(
				'INSERT INTO entries'
				'(kind, currency, amount, occurred_at, comment, category_id, profile_id) '
				'VALUES (?, ?, ?, ?, ?, ?, ?)',
				(*values, profile_id),
			).lastrowid
			assert entry_id is not None  # The INSERT always creates a row id.
		else:
			dbo.execute(
				'UPDATE entries SET kind = ?, currency = ?, amount = ?, '
				'occurred_at = ?, comment = ?, category_id = ? '
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
	category = fetch_owned('categories', category_id, profile_id)
	if category['is_default']:
		raise ValidationError('Категорию «Без категории» нельзя удалить.')

	with db.get() as dbo:
		default_id = dbo.execute(
			'SELECT id FROM categories WHERE profile_id = ? AND is_default = 1',
			(profile_id,)
		).fetchone()['id']
		dbo.execute('UPDATE entries SET category_id=? WHERE category_id=? AND profile_id=?', (default_id, category_id, profile_id))
		dbo.execute('DELETE FROM categories WHERE id=? AND profile_id=?', (category_id, profile_id))

def fetch_entries_for(profile_id: int) -> list[Entry]:
	dbo = db.get()
	entries = cast(list[Entry], [dict(r) for r in dbo.execute(
		'SELECT e.*, c.name AS category '
		'FROM entries e '
		'JOIN categories c ON c.id = e.category_id AND c.profile_id = e.profile_id '
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
	return entries

def fetch_balances(profile_id: int, entries: Iterable[Entry]) -> dict[str, int]:
	result: dict[str, int] = {
		r['currency']: r['amount'] for r in db.get().execute(
			'SELECT * FROM capitals WHERE profile_id = ?', (profile_id,)
		)}

	for entry in entries:
		currency, amount = entry['currency'], entry['amount']
		if entry['kind'] == 'income':
			result[currency] += amount
		else:
			result[currency] -= amount

	return result

def calc_statistics(
	entries: Iterable[Entry],
	period: str,
	selected: str,
	currency: str
) -> Statistics:
	try:
		if period == 'day':
			key = datetime.strptime(selected, '%Y-%m-%d').strftime('%Y-%m-%d')
		elif period == 'month':
			key = datetime.strptime(selected, '%Y-%m').strftime('%Y-%m')
		elif period == 'year' and re.fullmatch(r'\d{4}', selected) and 1 <= int(selected) <= 9999:
			key = selected
		else:
			raise ValueError()
	except ValueError:
		raise ValidationError('Укажите корректный период статистики.') from None
	
	matched = [
		e for e in entries if e['currency'] == currency and e['occurred_at'].startswith(key)
	]
	totals = {k: sum(
		e['amount'] for e in matched if e['kind'] == k
	) for k in ('income', 'expense')}

	groups: dict[str, dict[str, Breakdown]] = {'categories': {}, 'tags': {}}
	for e in matched:
		for group, labels in (
			('categories', [e['category']]),
			('tags', [t['name'] for t in e['tags']])
		):
			for label in labels:
				bucket = groups[group].setdefault(
					label, {'name': label, 'income': 0, 'expense': 0}
				)
				bucket[e['kind']] += e['amount']
	return {
		'totals': totals,
		'count': len(matched),
		'entries': matched,
		'categories': sorted(
			groups['categories'].values(),
			key=lambda r: r['income'] + r['expense'], reverse=True
		),
		'tags': sorted(
			groups['tags'].values(),
			key=lambda r: r['income'] + r['expense'], reverse=True
		)
	}
