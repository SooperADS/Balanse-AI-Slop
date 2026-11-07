"""Проверки учёта и HTTP-форм на изолированных временных базах SQLite."""

import re
import sqlite3
import tempfile
import unittest

from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypeAlias, cast

from flask.testing import FlaskClient
from werkzeug.test import TestResponse
from werkzeug.datastructures import MultiDict
from werkzeug.security import generate_password_hash

from app import init, COOKIE
from app import db
from app import config
from app.domain import (
	ValidationError,
	fetch_balances,
	fetch_entries_for,
	to_money,
	calc_statistics
)

# Возможные значения скалярного SQL-результата в тестовых запросах.
SQLiteValue: TypeAlias = str | int | float | bytes | None

class BudgetTest(unittest.TestCase):
	"""Изолированные проверки правил учёта, форм и доступа на временной SQLite-базе."""
	def setUp(self) -> None:
		"""Создать временную базу, войти и добавить исходный профиль с капиталом."""
		self.temp = tempfile.TemporaryDirectory()
		self.config = {
			'TESTING': True,
			'DATABASE': str(Path(self.temp.name) / 'test.sqlite3'),
			'PASSWORD_HASH': generate_password_hash(
				'test-password', method='pbkdf2:sha256:1000'
			),
			'PASSWORD_FINGERPRINT': config.token_hash('test-password')}
		self.app = init(self.config)
		self.client = self.app.test_client()

		self.login()
		self.post('/profiles/new', {
			'name': 'Семья', 'RUB': '1000', 'USD': '10', 'EUR': '0'
		})

	def tearDown(self) -> None:
		"""Удалить временную базу и её папку."""
		self.temp.cleanup()

	def csrf(self, client: FlaskClient | None = None, page: str = '/') -> str:
		"""Извлечь CSRF-токен страницы выбранного тестового клиента."""
		response = (client or self.client).get(page, follow_redirects=True)
		match = re.search(
			r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)
		)
		self.assertIsNotNone(match, response.get_data(as_text=True)[:1000])
		assert match is not None
		return match.group(1)

	def login(
		self, client: FlaskClient | None = None, password: str = 'test-password'
	) -> TestResponse:
		"""Войти тестовым клиентом с предварительным получением CSRF-cookie."""
		client = client or self.client
		return client.post('/login', data={
			'csrf_token': self.csrf(client, '/login'), 'password': password
		})

	def post(
		self,
		path: str,
		data: Mapping[str, str | int] | MultiDict[str, str] | None = None,
		client: FlaskClient | None = None,
	) -> TestResponse:
		"""Отправить форму с актуальным CSRF-токеном, сохраняя повторяющиеся ключи."""
		client = client or self.client
		form: MultiDict[str, str | int] = MultiDict(data or {})
		form['csrf_token'] = self.csrf(client)
		return client.post(path, data=form)

	def entry(self, **updates: str) -> dict[str, str]:
		"""Сформировать корректную обычную операцию с подменой указанных полей."""
		form = {
			'kind': 'expense',
			'amount': '12,34',
			'currency': 'RUB',
			'occurred_at': '2020-05-12T12:30:00',
			'comment': 'Продукты'
		}
		form.update(updates)
		return form

	def scalar(self, query: str, args: tuple[SQLiteValue, ...] = ()) -> SQLiteValue:
		"""Выполнить тестовый SQL-запрос и вернуть первое скалярное значение."""
		with self.app.app_context():
			return cast(SQLiteValue, db.get().execute(query, args).fetchone()[0])

	def add_category(self, name: str = 'Продукты') -> int:
		"""Добавить категорию через форму и вернуть её ID."""
		response = self.post('/library/categories/add', {'name': name})
		self.assertEqual(response.status_code, 302)
		return cast(int, self.scalar(
			'SELECT id FROM categories WHERE name = ?', (name,)
		))

	def add_tag(self, name: str = 'дом') -> int:
		"""Добавить тег через форму и вернуть его ID."""
		response = self.post('/library/tags/add', {'name': name})
		self.assertEqual(response.status_code, 302)
		return cast(int, self.scalar('SELECT id FROM tags WHERE name = ?', (name,)))

	def test_pages_and_headers(self) -> None:
		"""Проверить доступность страниц, ресурсов и защитные HTTP-заголовки."""
		for path in (
			'/',
			'/entries',
			'/entries/new',
			'/library',
			'/settings',
			'/profiles/new',
			'/?period=day&date=2020-05-12',
			'/?period=year&date=2020&currency=EUR'
		):
			with self.subTest(path=path):
				response = self.client.get(path)
				self.assertEqual(response.status_code, 200)
				self.assertIn('no-store', response.headers['Cache-Control'])
				self.assertIn(
					"frame-ancestors 'none'",
					response.headers['Content-Security-Policy']
				)

		for path in ('/static/style.css', '/static/app.js', '/static/favicon.svg'):
			response = self.client.get(path)
			self.assertEqual(response.status_code, 200)
			response.close()

	def test_exact_money_and_independent_balances(self) -> None:
		"""Проверить точность сотых и независимость балансов валют."""
		for form in (
			self.entry(amount='0.10'),
			self.entry(amount='0.20'),
			self.entry(kind='income', amount='4.56', currency='USD')
		):
			self.assertEqual(
				self.post('/entries/new', form).status_code, 302
			)

		with self.app.app_context():
			self.assertEqual(fetch_balances(
				1, fetch_entries_for(1)), {
					'RUB': 99970, 'USD': 1456, 'EUR': 0
				}
			)

		self.assertEqual(to_money('1 234,56'), 123456)
		for invalid in ('0', '-1', '1.001', 'NaN', 'Infinity', '1e3', 'abc'):
			with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
				to_money(invalid)

	def test_required_comment_future_date_and_time(self) -> None:
		"""Проверить обязательный комментарий и отбраковку неверных сумм, типов и дат."""
		for changes in (
			{'comment': '  '},
			{'amount': '0'},
			{'amount': '1.999'},
			{'currency': 'GBP'},
			{'kind': 'transfer'},
			{'occurred_at': '2020-02-30T12:00'},
			{'occurred_at': '2020-01-01'},
			{'occurred_at': '2020-01-01T12:00+03:00'}
		):
			with self.subTest(changes=changes):
				response = self.post(
					'/entries/new', self.entry(**changes)
				)
				self.assertEqual(response.status_code, 422)
				self.assertIn(
					'Продукты' if 'comment' not in changes else 'Комментарий',
					response.get_data(as_text=True)
				)

		self.assertEqual(self.scalar('SELECT count(*) FROM entries'), 0)

	def test_edit_delete_and_tag_replacement(self) -> None:
		"""Проверить изменение операции, замену тегов и необратимое удаление."""
		tag = self.add_tag()
		self.assertEqual(self.post(
			'/entries/new', self.entry(tags=str(tag))
		).status_code, 302)
		self.assertEqual(self.scalar('SELECT count(*) FROM entry_tags'), 1)
		self.assertEqual(self.client.get('/entries/1/edit').status_code, 200)
		self.assertEqual(self.post(
			'/entries/1/edit',
			self.entry(kind='income', currency='EUR', amount='8.99', comment='Подарок')
		).status_code, 302)
		self.assertEqual(self.scalar('SELECT count(*) FROM entry_tags'), 0)

		with self.app.app_context():
			self.assertEqual(
				fetch_balances(1, fetch_entries_for(1)),
				{'RUB': 100000, 'USD': 1000, 'EUR': 899}
			)

		self.assertEqual(self.post('/entries/1/delete').status_code, 302)
		self.assertEqual(self.scalar('SELECT count(*) FROM entries'), 0)

	def test_category_deletion_sets_null_and_default_is_virtual(self) -> None:
		"""Проверить NULL после удаления категории и отсутствие строки категории по умолчанию."""
		self.assertEqual(self.scalar('SELECT count(*) FROM categories'), 0)
		category = self.add_category()
		self.post('/entries/new', self.entry(category_id=str(category)))
		self.assertEqual(self.post('/library/categories/delete', {'id': category}).status_code, 302)
		self.assertIsNone(self.scalar('SELECT category_id FROM entries'))
		self.assertEqual(self.scalar('SELECT count(*) FROM categories'), 0)
		self.assertIn('Без категории', self.client.get('/entries?category_id=none').get_data(as_text=True))
		self.assertEqual(self.post('/library/categories/add', {'name': 'Без категории'}).status_code, 422)
		with self.app.app_context():
			self.assertEqual(fetch_entries_for(1)[0]['category'], 'Без категории')

	def test_tag_deletion_keeps_entry(self) -> None:
		"""Проверить каскадное удаление связей тега с сохранением операций."""
		tag = self.add_tag()

		self.post('/entries/new', self.entry(tags=str(tag)))
		self.post('/library/tags/delete', {'id': tag})
		self.assertEqual(self.scalar('SELECT count(*) FROM entry_tags'), 0)
		self.assertEqual(self.scalar('SELECT count(*) FROM entries'), 1)

	def test_statistics_periods_and_overlapping_tags(self) -> None:
		"""Проверить периоды, разделение валют и повторный учёт операции по её тегам."""
		tag1, tag2 = self.add_tag('дом'), self.add_tag('еда')
		form = MultiDict(self.entry(amount='12.34'))
		form.setlist('tags', [str(tag1), str(tag2)])

		self.post('/entries/new', form)
		self.post('/entries/new', self.entry(
			kind='income', amount='50', occurred_at='2020-05-13T01:00'
		))
		self.post('/entries/new', self.entry(amount='80', currency='USD'))
		self.post('/entries/new', self.entry(
			amount='5', occurred_at='2019-12-31T23:59'
		))

		with self.app.app_context():
			entries = fetch_entries_for(1)
			day = calc_statistics(
				entries, 'day', '2020-05-12', 'RUB'
			)

			self.assertEqual(day['current']['totals'], {'income': 0, 'expense': 1234})
			self.assertEqual([r['expense'] for r in day['current']['tags']], [1234, 1234])
			
			month = calc_statistics(
				entries, 'month', '2020-05', 'RUB'
			)
			
			self.assertEqual(month['current']['totals'], {'income': 5000, 'expense': 1234})
			self.assertEqual(calc_statistics(
				entries, 'year', '2019', 'RUB'
			)['current']['totals']['expense'], 500)
			self.assertEqual(calc_statistics(
				entries, 'day', '2020-05-12', 'USD'
			)['current']['totals']['expense'], 8000)

		for url in (
			'/?period=unknown',
			'/?period=month&date=broken',
			'/?currency=GBP',
			'/entries?from=broken'
		):
			self.assertEqual(self.client.get(url).status_code, 400)

	def test_profile_isolation_and_foreign_keys(self) -> None:
		"""Проверить изоляцию данных профилей на уровне форм и составных внешних ключей."""
		cat = self.add_category('Секретная категория')
		tag = self.add_tag('Секретный тег')

		self.post('/entries/new', self.entry(comment='Запись первого профиля'))
		self.post('/profiles/new', {
			'name': 'Другой профиль', 'RUB': '0', 'USD': '0', 'EUR': '100'
		})
		
		page = self.client.get('/entries').get_data(as_text=True)
		self.assertNotIn('Запись первого профиля', page)
		for path in ('/entries/1/edit',):
			self.assertEqual(self.client.get(path).status_code, 404)

		self.assertEqual(
			self.post('/entries/1/edit', self.entry()).status_code, 404
		)
		self.assertEqual(self.post('/entries/1/delete').status_code, 404)
		self.assertEqual(
			self.post('/entries/new',
			self.entry(category_id=str(cat))).status_code,
			404
		)
		self.assertEqual(
			self.post('/entries/new', self.entry(tags=str(tag))).status_code,
			404
		)

		for kind, item in (('categories', cat), ('tags', tag)):
			self.assertEqual(
				self.post(f'/library/{kind}/delete', {'id': item}).status_code,
				404
			)

		with self.app.app_context():
			with self.assertRaises(sqlite3.IntegrityError), db.get():
				db.get().execute(
					"INSERT INTO entries"
					"(profile_id, currency, amount, occurred_at, comment, category_id) "
					"VALUES (2, 'RUB', -100, '2020-01-01T00:00:00', 'x', ?)",
					(cat,)
				)

		self.post('/profiles/switch', {'profile_id': 1})
		self.assertIn(
			'Запись первого профиля',
			self.client.get('/entries').get_data(as_text=True)
		)

	def test_capital_changes_and_profile_deletion(self) -> None:
		"""Проверить изменение капитала и удаление всех данных профиля."""
		tag = self.add_tag()

		self.post('/entries/new', self.entry(tags=str(tag)))
		self.assertEqual(self.post(
			'/settings', {'name': 'Новый', 'RUB': '200', 'USD': '0', 'EUR': '2.50'}
		).status_code, 302)

		with self.app.app_context():
			self.assertEqual(fetch_balances(
				1, fetch_entries_for(1)
			)['RUB'], 18766)

		self.assertEqual(self.post('/profiles/delete').status_code, 302)
		for table in ('profiles', 'entries', 'tags', 'categories', 'capitals', 'entry_tags'):
			self.assertEqual(
				self.scalar(f'SELECT count(*) FROM {table}'), 0, table
			)

		self.assertEqual(
			self.client.get('/', follow_redirects=True).status_code, 200
		)
		self.assertEqual(
			self.scalar('SELECT profile_id FROM auth_sessions'), None
		)

	def test_negative_initial_capital(self) -> None:
		"""Проверить отрицательный капитал и его отображение без ошибок знака."""
		self.assertEqual(self.post(
			'/settings', {
				'name': 'Долг',
				'RUB': '-0.01',
				'USD': '-123,45',
				'EUR': '0'
			}
		).status_code, 302)
		
		page = self.client.get('/settings').get_data(as_text=True)
		self.assertIn('value="-0.01"', page)
		self.assertIn('value="-123.45"', page)
		self.post('/entries/new', self.entry(kind='income', amount='10'))
		with self.app.app_context():
			self.assertEqual(
				fetch_balances(1, fetch_entries_for(1)),
				{'RUB': 999, 'USD': -12345, 'EUR': 0}
			)

		self.assertEqual(self.post('/profiles/new', {
			'name': 'Долг 2', 'RUB': '-1000', 'USD': '0', 'EUR': '-0.99'
		}).status_code, 302)

	def test_entry_filters(self) -> None:
		"""Проверить поиск, фильтры журнала и включительные границы дат."""
		cat, tag = self.add_category(), self.add_tag()

		self.post('/entries/new', self.entry(
			comment='Находится', category_id=str(cat), tags=str(tag)
		))
		self.post('/entries/new', self.entry(
			comment='Другой доход', kind='income', currency='USD'
		))

		for query in (
			'q=Находится',
			'kind=expense',
			'currency=RUB',
			f'category_id={cat}',
			f'tag={tag}',
			'from=2020-05-12&to=2020-05-12&currency=RUB'
		):
			page = self.client.get('/entries?' + query).get_data(as_text=True)
			self.assertIn('Находится', page)
			self.assertNotIn('Другой доход', page)
		
		self.assertNotIn(
			'Находится',
			self.client.get('/entries?from=2020-05-13').get_data(as_text=True)
		)

	def test_duplicates_rename_and_escaping(self) -> None:
		"""Проверить уникальность справочника, переименование и экранирование HTML."""
		cat = self.add_category()
		self.assertEqual(self.post(
			'/library/categories/add', {'name': 'Продукты'}
		).status_code, 422)
		self.assertEqual(self.post(
			'/library/categories/rename', {'id': cat, 'name': 'Еда'}
		).status_code, 302)
		self.post('/entries/new', self.entry(comment='<script>alert(1)</script>'))
		
		page = self.client.get('/entries').get_data(as_text=True)
		self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', page)
		self.assertNotIn('<script>alert(1)</script>', page)

	def test_auth_csrf_logout_and_password_invalidation(self) -> None:
		"""Проверить вход, CSRF, выход, сохранение сессий и отзыв при смене пароля."""
		outsider = self.app.test_client()
		self.assertEqual(outsider.get('/entries').status_code, 302)
		self.assertEqual(outsider.get(
			'/config.toml', follow_redirects=True
		).status_code, 200)
		self.assertNotIn('test-password', outsider.get('/config.toml', follow_redirects=True).get_data(as_text=True))
		self.assertEqual(outsider.post(
			'/login', data={'password': 'test-password'}
		).status_code, 400)
		self.assertEqual(
			self.login(outsider, 'wrong').status_code, 401
		)
		self.assertEqual(
			self.client.post('/entries/new', data=self.entry()).status_code, 400
		)
		self.assertEqual(self.client.post(
			'/entries/new', data={**self.entry(), 'csrf_token': 'неверный'}
		).status_code, 400)
		self.assertEqual(self.client.post(
			'/entries/new',
			data={**self.entry(), 'csrf_token': self.csrf()},
			headers={'Origin': 'http://evil.example'}
		).status_code, 400)
		self.assertEqual(self.scalar('SELECT count(*) FROM entries'), 0)
		
		cookie = self.client.get_cookie(COOKIE)
		assert cookie is not None
		token = cookie.value
		
		self.assertNotEqual(self.scalar(
			'SELECT token_hash FROM auth_sessions'), token
		)
		self.assertEqual(self.login(outsider).status_code, 302)
		self.assertEqual(self.post('/logout').status_code, 302)
		self.assertIsNone(self.client.get_cookie(COOKIE))
		self.assertEqual(self.client.get('/entries').status_code, 302)
		
		# A process restart preserves sessions; a password change invalidates them.
		restarted = init(self.config).test_client()
		outside_cookie = outsider.get_cookie(COOKIE)
		assert outside_cookie is not None
		restarted.set_cookie(COOKIE, outside_cookie.value)
		
		self.assertEqual(restarted.get('/entries').status_code, 200)
		changed = {
			**self.config,
			'PASSWORD_FINGERPRINT': config.token_hash('new-password'),
			'PASSWORD_HASH': generate_password_hash(
				'new-password', method='pbkdf2:sha256:1000'
			)
		}

		updated = init(changed).test_client()
		updated.set_cookie(COOKIE, outside_cookie.value)
		self.assertEqual(updated.get('/entries').status_code, 302)

	def test_login_rate_limit_and_no_login_csrf_cookie_on_success(self) -> None:
		"""Проверить ограничение входа и удаление CSRF-cookie после успешной авторизации."""
		client = self.app.test_client()
		for _ in range(5):
			self.assertEqual(self.login(client, 'wrong').status_code, 401)

		self.assertEqual(self.login(client).status_code, 429)
		with self.app.app_context(), db.get() as dbo:
			dbo.execute('UPDATE login_attempts SET window_started=0')

		response = self.login(client)
		self.assertEqual(response.status_code, 302)
		self.assertIsNone(client.get_cookie('budget_login_csrf'))

		cookie = next(
			c for c in response.headers.getlist('Set-Cookie') if c.startswith(COOKIE + '=')
		)
		self.assertIn('HttpOnly', cookie)
		self.assertIn('SameSite=Strict', cookie)
		self.assertIn('9999', cookie)

	def transfer(self, **updates: str) -> dict[str, str]:
		"""Сформировать корректный трансфер с подменой указанных полей."""
		return self.entry(**{
			'kind': 'transfer', 'amount': '900', 'currency': 'RUB',
			'destination_currency': 'USD', 'destination_amount': '10',
			'comment': 'Обмен валют', **updates,
		})

	def test_transfer_amounts_and_both_currency_filters(self) -> None:
		"""Проверить обе стороны трансфера, его отображение и фильтр каждой валюты."""
		self.assertEqual(self.post('/entries/new', self.transfer()).status_code, 302)
		with self.app.app_context():
			entries = fetch_entries_for(1)
			self.assertEqual(len(entries), 1)
			self.assertEqual(entries[0]['destination_amount'], 1000)
			self.assertEqual(fetch_balances(1, entries), {'RUB': 10000, 'USD': 2000, 'EUR': 0})
		for currency in ('RUB', 'USD'):
			page = self.client.get('/entries', query_string={'currency': currency}).get_data(as_text=True)
			self.assertIn('Обмен валют', page)
		self.assertNotIn('Обмен валют', self.client.get('/entries?currency=EUR').get_data(as_text=True))
		page = self.client.get('/entries?kind=transfer').get_data(as_text=True)
		self.assertIn('Обмен валют', page)
		self.assertIn('1 RUB', page)
		self.assertIn('USD', page)
		edit = self.client.get('/entries/1/edit')
		self.assertEqual(edit.status_code, 200)
		self.assertIn('value="10.00"', edit.get_data(as_text=True))

	def test_transfer_uses_only_final_amount_and_computes_rate(self) -> None:
		"""Проверить сохранение только сумм без курса и типа операции в БД."""
		self.assertEqual(self.post('/entries/new', self.transfer(
			currency='USD', destination_currency='RUB', amount='100',
			destination_amount='9000', exchange_rate='999', transfer_mode='invalid',
		)).status_code, 302)
		with self.app.app_context():
			entries = fetch_entries_for(1)
			self.assertEqual(entries[0]['amount'], -10000)
			self.assertEqual(entries[0]['destination_amount'], 900000)
			self.assertEqual(fetch_balances(1, entries), {'RUB': 1000000, 'USD': -9000, 'EUR': 0})
			columns = {row['name'] for row in db.get().execute('PRAGMA table_info(entries)')}
			self.assertNotIn('exchange_rate', columns)
			self.assertNotIn('kind', columns)
		page = self.client.get('/entries/1/edit').get_data(as_text=True)
		self.assertIn('value="9000.00"', page)
		self.assertIn('data-exchange-rate', page)
		self.assertNotIn('name="exchange_rate"', page)
		self.assertNotIn('transfer_mode', page)
		self.assertIn('90', self.client.get('/entries').get_data(as_text=True))

	def test_transfer_edit_delete_and_type_changes(self) -> None:
		"""Проверить пересчёт балансов при изменении, смене типа и удалении трансфера."""
		tag = self.add_tag()
		self.post('/entries/new', self.transfer(tags=str(tag)))
		self.assertEqual(self.post('/entries/1/edit', self.transfer(
			amount='500', destination_currency='EUR', destination_amount='5',
		)).status_code, 302)
		with self.app.app_context():
			self.assertEqual(fetch_balances(1, fetch_entries_for(1)), {'RUB': 50000, 'USD': 1000, 'EUR': 500})
		self.assertEqual(self.scalar('SELECT count(*) FROM entries'), 1)
		self.assertEqual(self.scalar('SELECT count(*) FROM entry_tags'), 0)
		self.assertEqual(self.post('/entries/1/edit', self.entry(kind='income', amount='5')).status_code, 302)
		self.assertIsNone(self.scalar('SELECT destination_currency FROM entries'))
		self.assertIsNone(self.scalar('SELECT destination_amount FROM entries'))
		self.assertEqual(self.post('/entries/1/edit', self.transfer()).status_code, 302)
		self.assertEqual(self.post('/entries/1/delete').status_code, 302)
		with self.app.app_context():
			self.assertEqual(fetch_balances(1, fetch_entries_for(1)), {'RUB': 100000, 'USD': 1000, 'EUR': 0})

	def test_transfer_validation_and_failed_edit_preserves_both_sides(self) -> None:
		"""Проверить неверные поля трансфера и сохранность записи после отказа."""
		self.post('/entries/new', self.transfer())
		for changes in (
			{'destination_currency': 'RUB'}, {'destination_currency': 'GBP'},
			{'destination_amount': '0'}, {'destination_amount': '-1'},
			{'destination_amount': '0.001'}, {'destination_amount': ''},
		):
			with self.subTest(changes=changes):
				self.assertEqual(self.post('/entries/1/edit', self.transfer(**changes)).status_code, 422)
		self.assertEqual(self.scalar('SELECT amount FROM entries'), -90000)
		self.assertEqual(self.scalar('SELECT destination_amount FROM entries'), 1000)
		self.assertEqual(self.scalar('SELECT count(*) FROM entries'), 1)

	def test_transfer_statistics_excludes_conversion_from_income_and_expense(self) -> None:
		"""Проверить отдельный учёт конвертации без ложных доходов и расходов."""
		tag = self.add_tag()
		self.post('/entries/new', self.transfer(tags=str(tag)))
		self.post('/entries/new', self.entry(kind='income', amount='12'))
		with self.app.app_context():
			entries = fetch_entries_for(1)
			rub = calc_statistics(entries, 'day', '2020-05-12', 'RUB')
			usd = calc_statistics(entries, 'day', '2020-05-12', 'USD')
			self.assertEqual(rub['current']['totals'], {'income': 1200, 'expense': 0})
			self.assertEqual(rub['current']['transfers'], {'sent': 90000, 'received': 0, 'count': 1})
			self.assertEqual(rub['current']['tags'], [])
			self.assertEqual(rub['current']['categories'][0]['income'], 1200)
			self.assertEqual(usd['current']['totals'], {'income': 0, 'expense': 0})
			self.assertEqual(usd['current']['transfers'], {'sent': 0, 'received': 1000, 'count': 1})
			self.assertEqual(usd['current']['categories'], [])
			self.assertEqual(calc_statistics(entries, 'day', '2020-05-12', 'EUR')['current']['count'], 0)
		for currency in ('RUB', 'USD'):
			page = self.client.get('/?period=day&date=2020-05-12&currency=' + currency)
			self.assertEqual(page.status_code, 200)
			self.assertIn('Обмен валют', page.get_data(as_text=True))

	def test_arbitrary_period_includes_both_boundaries(self) -> None:
		"""Проверить произвольный диапазон через границы года и високосный день."""
		for timestamp in ('2019-12-30T23:59:59', '2019-12-31T00:00:00', '2020-01-01T12:30:00',
		                  '2020-02-29T23:59:59', '2020-03-01T00:00:00'):
			self.assertEqual(self.post('/entries/new', self.entry(amount='1', occurred_at=timestamp)).status_code, 302)
		with self.app.app_context():
			entries = fetch_entries_for(1)
			stats = calc_statistics(entries, 'range', '2019-12-31', 'RUB', '2020-02-29')
			self.assertEqual(stats['current']['count'], 3)
			self.assertEqual(stats['current']['totals']['expense'], 300)
			self.assertEqual((stats['start_date'], stats['end_date']), ('2019-12-31', '2020-02-29'))
			self.assertEqual(calc_statistics(entries, 'range', '2020-02-29', 'RUB', '2020-02-29')['current']['count'], 1)
		for start, end in (('2020-02-30', '2020-03-01'), ('2020-03-01', '2020-02-29'), ('2020-01-01', 'bad')):
			self.assertEqual(self.client.get('/', query_string={'period': 'range', 'date': start, 'end_date': end}).status_code, 400)
		page = self.client.get('/?period=range&date=2019-12-31&end_date=2020-02-29')
		self.assertEqual(page.status_code, 200)
		self.assertIn('29.02.2020', page.get_data(as_text=True))

	def test_week_is_last_seven_days_inclusive(self) -> None:
		"""Проверить семидневный период через границу года с обеими включёнными датами."""
		for timestamp in ('2019-12-27T23:59:59', '2019-12-28T00:00:00', '2020-01-03T23:59:59', '2020-01-04T00:00:00'):
			self.post('/entries/new', self.entry(amount='1', occurred_at=timestamp))
		with self.app.app_context():
			stats = calc_statistics(fetch_entries_for(1), 'week', '2020-01-03', 'RUB')
			self.assertEqual((stats['start_date'], stats['end_date']), ('2019-12-28', '2020-01-03'))
			self.assertEqual(stats['current']['totals']['expense'], 200)
			self.assertEqual(stats['current']['count'], 2)
		page = self.client.get('/?period=week&date=2020-01-03')
		self.assertEqual(page.status_code, 200)
		self.assertIn('Последние 7 дней', page.get_data(as_text=True))

	def test_multiple_tag_filter_requires_all_tags(self) -> None:
		"""Проверить отбор по всем тегам, дополнительные теги и повторяющиеся параметры."""
		tag1, tag2, tag3 = self.add_tag('дом'), self.add_tag('ремонт'), self.add_tag('ещё')
		for comment, selected in (
			('Все выбранные', [tag1, tag2, tag3]),
			('Только первый', [tag1]), ('Только второй', [tag2]), ('Без тегов', []),
		):
			form = MultiDict(self.entry(comment=comment))
			form.setlist('tags', [str(tag) for tag in selected])
			self.post('/entries/new', form)
		query = [('tag', str(tag1)), ('tag', str(tag2))]
		page = self.client.get('/entries', query_string=query).get_data(as_text=True)
		self.assertIn('Все выбранные', page)
		for excluded in ('Только первый', 'Только второй', 'Без тегов'):
			self.assertNotIn(excluded, page)
		self.assertIn('Все выбранные', self.client.get('/entries', query_string=query + [('tag', str(tag1))]).get_data(as_text=True))
		self.assertNotIn('Все выбранные', self.client.get('/entries', query_string=query + [('tag', '99999')]).get_data(as_text=True))
		self.assertIn('Только первый', self.client.get('/entries').get_data(as_text=True))

	def test_schema_rejects_incomplete_or_invalid_transfer(self) -> None:
		"""Проверить ограничения SQL для неполных трансферов, знака и нулевой суммы."""
		with self.app.app_context():
			for amount, currency, received in ((-100, 'RUB', 100), (-100, 'USD', None), (100, 'USD', 100), (-100, None, 100), (0, None, None)):
				with self.subTest(amount=amount, currency=currency, received=received), self.assertRaises(sqlite3.IntegrityError), db.get():
					db.get().execute(
						"INSERT INTO entries(profile_id,currency,amount,occurred_at,comment,destination_currency,destination_amount) VALUES (1,'RUB',?,'2020-01-01T00:00:00','x',?,?)",
						(amount, currency, received),
					)

	def test_planned_records_do_not_change_current_balances(self) -> None:
		"""Проверить будущие операции и исключение обеих сторон трансфера из текущего баланса."""
		future = (datetime.now() + timedelta(days=2)).isoformat(timespec='seconds')
		self.assertEqual(self.post('/entries/new', self.entry(amount='50', occurred_at=future, comment='Будущая покупка')).status_code, 302)
		self.assertEqual(self.post('/entries/new', self.transfer(occurred_at=future)).status_code, 302)
		with self.app.app_context():
			entries = fetch_entries_for(1)
			self.assertTrue(all(e['planned'] for e in entries))
			self.assertEqual(fetch_balances(1, entries), {'RUB': 100000, 'USD': 1000, 'EUR': 0})
			self.assertEqual(fetch_balances(1, entries, now=datetime.now() + timedelta(days=3)), {'RUB': 5000, 'USD': 2000, 'EUR': 0})
		page = self.client.get('/entries').get_data(as_text=True)
		self.assertIn('Запланированные записи', page)
		self.assertIn('Будущая покупка', page)
		self.assertIn('planned-row', page)
		self.assertNotIn('data-now-max', self.client.get('/entries/new').get_data(as_text=True))

	def test_statistics_split_by_time_and_transition_at_exact_instant(self) -> None:
		"""Проверить разделение статистики и переход в текущие ровно в назначенное время."""
		self.post('/entries/new', self.entry(kind='income', amount='20', occurred_at='2020-05-12T12:30:00'))
		self.post('/entries/new', self.entry(amount='10', occurred_at='2020-05-12T12:30:01'))
		self.post('/entries/new', self.transfer(occurred_at='2020-05-12T12:30:01'))
		with self.app.app_context():
			entries = fetch_entries_for(1)
			stats = calc_statistics(entries, 'day', '2020-05-12', 'RUB', now=datetime(2020,5,12,12,30,0))
			self.assertEqual(stats['current']['totals'], {'income': 2000, 'expense': 0})
			self.assertEqual(stats['planned']['totals'], {'income': 0, 'expense': 1000})
			self.assertEqual(stats['planned']['transfers']['sent'], 90000)
			self.assertEqual(stats['planned']['count'], 2)
			self.assertEqual(stats['current']['count'], 1)
			after = calc_statistics(entries, 'day', '2020-05-12', 'RUB', now=datetime(2020,5,12,12,30,1))
			self.assertEqual(after['planned']['count'], 0)
			self.assertEqual(after['current']['count'], 3)
			self.assertEqual(after['current']['totals']['expense'], 1000)

	def test_signed_amounts_and_nullable_category(self) -> None:
		"""Проверить знаковое хранение сумм, вычисление типа и nullable-категорию."""
		for kind in ('income', 'expense'):
			self.assertEqual(self.post('/entries/new', self.entry(kind=kind, amount='1.23')).status_code, 302)
		self.assertEqual(self.scalar('SELECT amount FROM entries WHERE id=1'), 123)
		self.assertEqual(self.scalar('SELECT amount FROM entries WHERE id=2'), -123)
		self.assertIsNone(self.scalar('SELECT category_id FROM entries WHERE id=1'))
		self.assertEqual(self.scalar('SELECT count(*) FROM categories'), 0)
		with self.app.app_context():
			entries = fetch_entries_for(1)
			self.assertEqual([e['kind'] for e in entries], ['expense', 'income'])
		self.assertIn('value="1.23"', self.client.get('/entries/2/edit').get_data(as_text=True))

if __name__ == '__main__':
	unittest.main()
