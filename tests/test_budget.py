from datetime import datetime, timedelta
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from werkzeug.datastructures import MultiDict
from werkzeug.security import generate_password_hash

from budget.auth import COOKIE, token_hash
from budget.db import get_db
from budget.domain import (ValidationError, balances, create_profile, entries_for,
						money, save_entry, statistics)
from budget.web import create_app


class BudgetTest(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.config = {'TESTING': True, 'DATABASE': str(Path(self.temp.name) / 'test.sqlite3'),
					'PASSWORD_HASH': generate_password_hash('test-password', method='pbkdf2:sha256:1000'),
					'PASSWORD_FINGERPRINT': token_hash('test-password')}
		self.app = create_app(self.config)
		self.client = self.app.test_client()
		self.login()
		self.post('/profiles/new', {'name': 'Семья', 'RUB': '1000', 'USD': '10', 'EUR': '0'})

	def tearDown(self):
		self.temp.cleanup()

	def csrf(self, client=None, page='/'):
		response = (client or self.client).get(page, follow_redirects=True)
		match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
		self.assertIsNotNone(match, response.get_data(as_text=True)[:1000])
		return match.group(1)

	def login(self, client=None, password='test-password'):
		client = client or self.client
		return client.post('/login', data={'csrf_token': self.csrf(client, '/login'), 'password': password})

	def post(self, path, data=None, client=None):
		client = client or self.client
		form = MultiDict(data or {})
		form['csrf_token'] = self.csrf(client)
		return client.post(path, data=form)

	def entry(self, **updates):
		form = {'kind': 'expense', 'amount': '12,34', 'currency': 'RUB',
				'occurred_at': '2020-05-12T12:30:00', 'comment': 'Продукты'}
		form.update(updates)
		return form

	def scalar(self, query, args=()):
		with self.app.app_context():
			return get_db().execute(query, args).fetchone()[0]

	def add_category(self, name='Продукты'):
		response = self.post('/library/categories/add', {'name': name})
		self.assertEqual(response.status_code, 302)
		return self.scalar('SELECT id FROM categories WHERE name=?', (name,))

	def add_tag(self, name='дом'):
		response = self.post('/library/tags/add', {'name': name})
		self.assertEqual(response.status_code, 302)
		return self.scalar('SELECT id FROM tags WHERE name=?', (name,))

	def test_pages_and_headers(self):
		for path in ('/', '/entries', '/entries/new', '/library', '/settings', '/profiles/new',
					'/?period=day&date=2020-05-12', '/?period=year&date=2020&currency=EUR'):
			with self.subTest(path=path):
				response = self.client.get(path)
				self.assertEqual(response.status_code, 200)
				self.assertIn('no-store', response.headers['Cache-Control'])
				self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
		for path in ('/static/style.css', '/static/app.js', '/static/favicon.svg'):
			response = self.client.get(path)
			self.assertEqual(response.status_code, 200)
			response.close()

	def test_exact_money_and_independent_balances(self):
		for form in (self.entry(amount='0.10'), self.entry(amount='0.20'),
					self.entry(kind='income', amount='4.56', currency='USD')):
			self.assertEqual(self.post('/entries/new', form).status_code, 302)
		with self.app.app_context():
			self.assertEqual(balances(1, entries_for(1)), {'RUB': 99970, 'USD': 1456, 'EUR': 0})
		self.assertEqual(money('1 234,56'), 123456)
		for invalid in ('0', '-1', '1.001', 'NaN', 'Infinity', '1e3', 'abc'):
			with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
				money(invalid)

	def test_required_comment_future_date_and_time(self):
		for changes in ({'comment': '  '}, {'amount': '0'}, {'amount': '1.999'},
						{'currency': 'GBP'}, {'kind': 'transfer'}, {'occurred_at': '2020-02-30T12:00'},
						{'occurred_at': (datetime.now() + timedelta(days=1)).isoformat(timespec='seconds')},
						{'occurred_at': (datetime.now() + timedelta(minutes=2)).isoformat(timespec='seconds')},
						{'occurred_at': '2020-01-01'}, {'occurred_at': '2020-01-01T12:00+03:00'}):
			with self.subTest(changes=changes):
				response = self.post('/entries/new', self.entry(**changes))
				self.assertEqual(response.status_code, 422)
				self.assertIn('Продукты' if 'comment' not in changes else 'Комментарий', response.get_data(as_text=True))
		self.assertEqual(self.scalar('SELECT count(*) FROM entries'), 0)

	def test_edit_delete_and_tag_replacement(self):
		tag = self.add_tag()
		self.assertEqual(self.post('/entries/new', self.entry(tags=str(tag))).status_code, 302)
		self.assertEqual(self.scalar('SELECT count(*) FROM entry_tags'), 1)
		self.assertEqual(self.client.get('/entries/1/edit').status_code, 200)
		self.assertEqual(self.post('/entries/1/edit', self.entry(kind='income', currency='EUR', amount='8.99', comment='Подарок')).status_code, 302)
		self.assertEqual(self.scalar('SELECT count(*) FROM entry_tags'), 0)
		with self.app.app_context():
			self.assertEqual(balances(1, entries_for(1)), {'RUB': 100000, 'USD': 1000, 'EUR': 899})
		self.assertEqual(self.post('/entries/1/delete').status_code, 302)
		self.assertEqual(self.scalar('SELECT count(*) FROM entries'), 0)

	def test_category_deletion_reassigns_and_default_is_protected(self):
		category = self.add_category()
		self.post('/entries/new', self.entry(category_id=str(category)))
		default = self.scalar('SELECT id FROM categories WHERE is_default=1')
		self.assertEqual(self.post('/library/categories/delete', {'id': category}).status_code, 302)
		self.assertEqual(self.scalar('SELECT category_id FROM entries'), default)
		for action in ('delete', 'rename'):
			self.assertEqual(self.post(f'/library/categories/{action}', {'id': default, 'name': 'Другое'}).status_code, 422)
		self.assertEqual(self.scalar('SELECT name FROM categories WHERE id=?', (default,)), 'Без категории')

	def test_tag_deletion_keeps_entry(self):
		tag = self.add_tag()
		self.post('/entries/new', self.entry(tags=str(tag)))
		self.post('/library/tags/delete', {'id': tag})
		self.assertEqual(self.scalar('SELECT count(*) FROM entry_tags'), 0)
		self.assertEqual(self.scalar('SELECT count(*) FROM entries'), 1)

	def test_statistics_periods_and_overlapping_tags(self):
		tag1, tag2 = self.add_tag('дом'), self.add_tag('еда')
		form = MultiDict(self.entry(amount='12.34'))
		form.setlist('tags', [str(tag1), str(tag2)])
		self.post('/entries/new', form)
		self.post('/entries/new', self.entry(kind='income', amount='50', occurred_at='2020-05-13T01:00'))
		self.post('/entries/new', self.entry(amount='80', currency='USD'))
		self.post('/entries/new', self.entry(amount='5', occurred_at='2019-12-31T23:59'))
		with self.app.app_context():
			entries = entries_for(1)
			day = statistics(entries, 'day', '2020-05-12', 'RUB')
			self.assertEqual(day['totals'], {'income': 0, 'expense': 1234})
			self.assertEqual([r['expense'] for r in day['tags']], [1234, 1234])
			month = statistics(entries, 'month', '2020-05', 'RUB')
			self.assertEqual(month['totals'], {'income': 5000, 'expense': 1234})
			self.assertEqual(statistics(entries, 'year', '2019', 'RUB')['totals']['expense'], 500)
			self.assertEqual(statistics(entries, 'day', '2020-05-12', 'USD')['totals']['expense'], 8000)
		for url in ('/?period=week', '/?period=month&date=broken', '/?currency=GBP', '/entries?from=broken'):
			self.assertEqual(self.client.get(url).status_code, 400)

	def test_profile_isolation_and_foreign_keys(self):
		cat = self.add_category('Секретная категория')
		tag = self.add_tag('Секретный тег')
		self.post('/entries/new', self.entry(comment='Запись первого профиля'))
		self.post('/profiles/new', {'name': 'Другой профиль', 'RUB': '0', 'USD': '0', 'EUR': '100'})
		page = self.client.get('/entries').get_data(as_text=True)
		self.assertNotIn('Запись первого профиля', page)
		for path in ('/entries/1/edit',):
			self.assertEqual(self.client.get(path).status_code, 404)
		self.assertEqual(self.post('/entries/1/edit', self.entry()).status_code, 404)
		self.assertEqual(self.post('/entries/1/delete').status_code, 404)
		self.assertEqual(self.post('/entries/new', self.entry(category_id=str(cat))).status_code, 404)
		self.assertEqual(self.post('/entries/new', self.entry(tags=str(tag))).status_code, 404)
		for kind, item in (('categories', cat), ('tags', tag)):
			self.assertEqual(self.post(f'/library/{kind}/delete', {'id': item}).status_code, 404)
		with self.app.app_context():
			with self.assertRaises(sqlite3.IntegrityError), get_db():
				get_db().execute("INSERT INTO entries(profile_id,kind,currency,amount,occurred_at,comment,category_id) VALUES (2,'expense','RUB',100,'2020-01-01T00:00:00','x',?)", (cat,))
		self.post('/profiles/switch', {'profile_id': 1})
		self.assertIn('Запись первого профиля', self.client.get('/entries').get_data(as_text=True))

	def test_capital_changes_and_profile_deletion(self):
		tag = self.add_tag()
		self.post('/entries/new', self.entry(tags=str(tag)))
		self.assertEqual(self.post('/settings', {'name': 'Новый', 'RUB': '200', 'USD': '0', 'EUR': '2.50'}).status_code, 302)
		with self.app.app_context():
			self.assertEqual(balances(1, entries_for(1))['RUB'], 18766)
		self.assertEqual(self.post('/profiles/delete').status_code, 302)
		for table in ('profiles', 'entries', 'tags', 'categories', 'capitals', 'entry_tags'):
			self.assertEqual(self.scalar(f'SELECT count(*) FROM {table}'), 0, table)
		self.assertEqual(self.client.get('/', follow_redirects=True).status_code, 200)
		self.assertEqual(self.scalar('SELECT profile_id FROM auth_sessions'), None)

	def test_negative_initial_capital(self):
		self.assertEqual(self.post('/settings', {'name': 'Долг', 'RUB': '-0.01', 'USD': '-123,45', 'EUR': '0'}).status_code, 302)
		page = self.client.get('/settings').get_data(as_text=True)
		self.assertIn('value="-0.01"', page)
		self.assertIn('value="-123.45"', page)
		self.post('/entries/new', self.entry(kind='income', amount='10'))
		with self.app.app_context():
			self.assertEqual(balances(1, entries_for(1)), {'RUB': 999, 'USD': -12345, 'EUR': 0})
		self.assertEqual(self.post('/profiles/new', {'name': 'Долг 2', 'RUB': '-1000', 'USD': '0', 'EUR': '-0.99'}).status_code, 302)

	def test_entry_filters(self):
		cat, tag = self.add_category(), self.add_tag()
		self.post('/entries/new', self.entry(comment='Находится', category_id=str(cat), tags=str(tag)))
		self.post('/entries/new', self.entry(comment='Другой доход', kind='income', currency='USD'))
		for query in ('q=Находится', 'kind=expense', 'currency=RUB', f'category_id={cat}', f'tag={tag}',
					'from=2020-05-12&to=2020-05-12&currency=RUB'):
			page = self.client.get('/entries?' + query).get_data(as_text=True)
			self.assertIn('Находится', page)
			self.assertNotIn('Другой доход', page)
		self.assertNotIn('Находится', self.client.get('/entries?from=2020-05-13').get_data(as_text=True))

	def test_duplicates_rename_and_escaping(self):
		cat = self.add_category()
		self.assertEqual(self.post('/library/categories/add', {'name': 'Продукты'}).status_code, 422)
		self.assertEqual(self.post('/library/categories/rename', {'id': cat, 'name': 'Еда'}).status_code, 302)
		self.post('/entries/new', self.entry(comment='<script>alert(1)</script>'))
		page = self.client.get('/entries').get_data(as_text=True)
		self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', page)
		self.assertNotIn('<script>alert(1)</script>', page)

	def test_auth_csrf_logout_and_password_invalidation(self):
		outsider = self.app.test_client()
		self.assertEqual(outsider.get('/entries').status_code, 302)
		self.assertEqual(outsider.get('/config.toml', follow_redirects=True).status_code, 200)
		self.assertNotIn('test-password', outsider.get('/config.toml', follow_redirects=True).get_data(as_text=True))
		self.assertEqual(outsider.post('/login', data={'password': 'test-password'}).status_code, 400)
		self.assertEqual(self.login(outsider, 'wrong').status_code, 401)
		self.assertEqual(self.client.post('/entries/new', data=self.entry()).status_code, 400)
		self.assertEqual(self.client.post('/entries/new', data={**self.entry(), 'csrf_token': 'неверный'}).status_code, 400)
		self.assertEqual(self.client.post('/entries/new', data={**self.entry(), 'csrf_token': self.csrf()}, headers={'Origin': 'http://evil.example'}).status_code, 400)
		self.assertEqual(self.scalar('SELECT count(*) FROM entries'), 0)
		token = self.client.get_cookie(COOKIE).value
		self.assertNotEqual(self.scalar('SELECT token_hash FROM auth_sessions'), token)
		self.assertEqual(self.login(outsider).status_code, 302)
		self.assertEqual(self.post('/logout').status_code, 302)
		self.assertIsNone(self.client.get_cookie(COOKIE))
		self.assertEqual(self.client.get('/entries').status_code, 302)
		# A process restart preserves sessions; a password change invalidates them.
		restarted = create_app(self.config).test_client()
		restarted.set_cookie(COOKIE, outsider.get_cookie(COOKIE).value)
		self.assertEqual(restarted.get('/entries').status_code, 200)
		changed = {**self.config, 'PASSWORD_FINGERPRINT': token_hash('new-password'),
				'PASSWORD_HASH': generate_password_hash('new-password', method='pbkdf2:sha256:1000')}
		updated = create_app(changed).test_client()
		updated.set_cookie(COOKIE, outsider.get_cookie(COOKIE).value)
		self.assertEqual(updated.get('/entries').status_code, 302)

	def test_login_rate_limit_and_no_login_csrf_cookie_on_success(self):
		client = self.app.test_client()
		for _ in range(5):
			self.assertEqual(self.login(client, 'wrong').status_code, 401)
		self.assertEqual(self.login(client).status_code, 429)
		with self.app.app_context(), get_db() as db:
			db.execute('UPDATE login_attempts SET window_started=0')
		response = self.login(client)
		self.assertEqual(response.status_code, 302)
		self.assertIsNone(client.get_cookie('budget_login_csrf'))
		cookie = next(c for c in response.headers.getlist('Set-Cookie') if c.startswith(COOKIE + '='))
		self.assertIn('HttpOnly', cookie)
		self.assertIn('SameSite=Strict', cookie)
		self.assertIn('9999', cookie)


if __name__ == '__main__':
	unittest.main()
