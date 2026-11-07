"""Валидация и операции с журналом. Денежные значения — целые сотые."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re

from werkzeug.exceptions import NotFound

from .db import get_db

CURRENCIES = ('RUB', 'USD', 'EUR')
SYMBOLS = {'RUB': '₽', 'USD': '$', 'EUR': '€'}

class ValidationError(ValueError):
	pass

def money(raw, *, capital=False):
	value = str(raw).strip().replace(' ', '').replace('\u00a0', '').replace(',', '.')
	pattern = r'-?\d+(?:\.\d{1,2})?' if capital else r'\d+(?:\.\d{1,2})?'
	if not re.fullmatch(pattern, value):
		raise ValidationError('Введите сумму числом с точностью до двух знаков после запятой.')
	try:
		result = int(Decimal(value) * 100)
	except (InvalidOperation, ValueError, OverflowError):
		raise ValidationError('Некорректная сумма.') from None
	if abs(result) > 900_000_000_000_000_000:
		raise ValidationError('Слишком большая сумма.')
	if not capital and result <= 0:
		raise ValidationError('Сумма операции должна быть больше нуля.')
	return result

def name(raw):
	value = raw.strip()
	if not 1 <= len(value) <= 80:
		raise ValidationError('Название должно содержать от 1 до 80 символов.')
	return value

def owned(table, item_id, profile_id):
	if table not in ('entries', 'categories', 'tags'):
		raise ValueError('Unsupported table')
	row = get_db().execute(
		f'SELECT * FROM {table} WHERE id = ? AND profile_id = ?',
		(item_id, profile_id),
	).fetchone()
	if row is None:
		raise NotFound()
	return row

def create_profile(raw_name, capitals):
	title = name(raw_name)
	amounts = {c: money(capitals.get(c, '0'), capital=True) for c in CURRENCIES}
	db = get_db()
	with db:
		profile_id = db.execute('INSERT INTO profiles(name) VALUES (?)', (title,)).lastrowid
		db.execute("INSERT INTO categories(profile_id, name, is_default) VALUES (?, 'Без категории', 1)", (profile_id,))
		db.executemany('INSERT INTO capitals VALUES (?, ?, ?)', [(profile_id, c, amounts[c]) for c in CURRENCIES])
	return profile_id

def save_profile(profile_id, raw_name, capitals):
	title = name(raw_name)
	amounts = {c: money(capitals.get(c, '0'), capital=True) for c in CURRENCIES}
	db = get_db()
	with db:
		db.execute('UPDATE profiles SET name = ? WHERE id = ?', (title, profile_id))
		db.executemany('UPDATE capitals SET amount = ? WHERE profile_id = ? AND currency = ?',
					[(amounts[c], profile_id, c) for c in CURRENCIES])

def save_entry(profile_id, form, entry_id=None, now=None):
	if entry_id is not None:
		owned('entries', entry_id, profile_id)
	amount = money(form.get('amount', ''))
	kind, currency = form.get('kind'), form.get('currency')
	if kind not in ('income', 'expense') or currency not in CURRENCIES:
		raise ValidationError('Выберите тип операции и валюту.')
	comment = form.get('comment', '').strip()
	if not 1 <= len(comment) <= 2000:
		raise ValidationError('Комментарий обязателен, не более 2000 символов.')
	raw_date = form.get('occurred_at', '')
	if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?', raw_date):
		raise ValidationError('Укажите корректные дату и время.')
	try:
		occurred_at = datetime.fromisoformat(raw_date)
	except ValueError:
		raise ValidationError('Укажите корректные дату и время.') from None
	if occurred_at > (now or datetime.now()):
		raise ValidationError('Нельзя добавлять операции с будущей датой или временем.')
	db = get_db()
	category_id = form.get('category_id')
	if not category_id:
		category_id = db.execute('SELECT id FROM categories WHERE profile_id = ? AND is_default = 1', (profile_id,)).fetchone()['id']
	owned('categories', category_id, profile_id)
	tags = set(form.getlist('tags'))
	for tag in tags:
		owned('tags', tag, profile_id)
	values = (kind, currency, amount, occurred_at.isoformat(timespec='seconds'), comment, category_id)
	with db:
		if entry_id is None:
			entry_id = db.execute(
				'INSERT INTO entries(kind, currency, amount, occurred_at, comment, category_id, profile_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
				(*values, profile_id),
			).lastrowid
		else:
			db.execute('UPDATE entries SET kind=?, currency=?, amount=?, occurred_at=?, comment=?, category_id=? WHERE id=? AND profile_id=?',
					(*values, entry_id, profile_id))
			db.execute('DELETE FROM entry_tags WHERE entry_id=? AND profile_id=?', (entry_id, profile_id))
		db.executemany('INSERT INTO entry_tags VALUES (?, ?, ?)', [(entry_id, tag, profile_id) for tag in tags])
	return entry_id

def delete_category(category_id, profile_id):
	category = owned('categories', category_id, profile_id)
	if category['is_default']:
		raise ValidationError('Категорию «Без категории» нельзя удалить.')
	db = get_db()
	default_id = db.execute('SELECT id FROM categories WHERE profile_id=? AND is_default=1', (profile_id,)).fetchone()['id']
	with db:
		db.execute('UPDATE entries SET category_id=? WHERE category_id=? AND profile_id=?', (default_id, category_id, profile_id))
		db.execute('DELETE FROM categories WHERE id=? AND profile_id=?', (category_id, profile_id))

def entries_for(profile_id):
	db = get_db()
	entries = [dict(r) for r in db.execute('SELECT e.*, c.name AS category FROM entries e JOIN categories c ON c.id=e.category_id AND c.profile_id=e.profile_id WHERE e.profile_id=? ORDER BY e.occurred_at DESC, e.id DESC', (profile_id,))]
	tags = {}
	for row in db.execute('SELECT et.entry_id, t.id, t.name FROM entry_tags et JOIN tags t ON t.id=et.tag_id AND t.profile_id=et.profile_id WHERE et.profile_id=? ORDER BY t.name', (profile_id,)):
		tags.setdefault(row['entry_id'], []).append(dict(row))
	for entry in entries:
		entry['tags'] = tags.get(entry['id'], [])
	return entries

def balances(profile_id, entries):
	result = {r['currency']: r['amount'] for r in get_db().execute('SELECT * FROM capitals WHERE profile_id=?', (profile_id,))}
	for entry in entries:
		result[entry['currency']] += entry['amount'] * (1 if entry['kind'] == 'income' else -1)
	return result

def statistics(entries, period, selected, currency):
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
	matched = [e for e in entries if e['currency'] == currency and e['occurred_at'].startswith(key)]
	totals = {k: sum(e['amount'] for e in matched if e['kind'] == k) for k in ('income', 'expense')}
	groups = {'categories': {}, 'tags': {}}
	for e in matched:
		for group, labels in (('categories', [e['category']]), ('tags', [t['name'] for t in e['tags']])):
			for label in labels:
				bucket = groups[group].setdefault(label, {'name': label, 'income': 0, 'expense': 0})
				bucket[e['kind']] += e['amount']
	return {'totals': totals, 'count': len(matched), 'entries': matched,
			**{group: sorted(rows.values(), key=lambda r: r['income'] + r['expense'], reverse=True) for group, rows in groups.items()}}
