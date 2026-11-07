"""Приложение Flask: обработчики страниц, общий вход, CSRF и контекст шаблонов."""

import sqlite3
import secrets
import hmac
import time

from datetime import date, datetime, time as day_time, timezone
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, Literal
from flask import Flask, abort, g, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from werkzeug.exceptions import HTTPException
from werkzeug.wrappers import Response
from werkzeug.security import check_password_hash

from app import db as dbm
from app import config
from app.domain import effective_rate
from app.domain import (
	Entry,
	CURRENCIES,
	SYMBOLS,
	ValidationError,
	fetch_balances,
	create_profile,
	delete_category,
	fetch_entries_for, 
	to_name, 
	fetch_owned, 
	save_entry,
	save_profile,
	calc_statistics
)

# Cookie постоянной серверной сессии; в БД хранится только хеш токена.
COOKIE: str = 'budget_session'
# Временный CSRF-токен формы входа, удаляемый после авторизации.
LOGIN_COOKIE: str = 'budget_login_csrf'

# Общий экземпляр с маршрутами; init() задаёт конфигурацию и создаёт таблицы.
APP: Flask = Flask(__name__)
APP.config.update(
	MAX_CONTENT_LENGTH=128 * 1024,  # Встроенный лимит Flask: 128 КиБ тела запроса.
)
APP.teardown_appcontext(dbm.close)
APP.add_template_filter(effective_rate, 'effective_rate')

@dataclass(frozen=True, slots=True)
class _Session:
	"""Проверенная сессия запроса без уже использованного отпечатка пароля."""

	token_hash: str
	csrf_token: str
	profile_id: int | None


def _find_session(token: str) -> _Session | None:
	"""Найти сессию текущего пароля; секрет токена не хранится в объекте."""
	row = dbm.get().execute(
		'SELECT token_hash, csrf_token, profile_id FROM auth_sessions '
		'WHERE token_hash=? AND password_fingerprint=?',
		(config.token_hash(token), config.get().password_fingerprint),
	).fetchone()
	return _Session(row['token_hash'], row['csrf_token'], row['profile_id']) if row else None


# ===================
#  Authorization
# ====================

@APP.before_request
def authenticate() -> Response | None:
	"""Задать единый момент запроса, найти сессию и проверить CSRF для POST."""
	g.now = datetime.now()
	g.auth = None
	g.profile = None
	g.error = None
	if request.endpoint == 'static':
		return None

	raw_token = request.cookies.get(COOKIE, '')
	if raw_token:
		g.auth = _find_session(raw_token)

	if g.auth is None:
		if request.endpoint != 'login':
			return redirect(url_for('login'))
		g.csrf = request.cookies.get(LOGIN_COOKIE) or secrets.token_urlsafe(32)
	else:
		g.csrf = g.auth.csrf_token

	if request.method == 'POST':
		cookie_present = g.auth is not None or bool(request.cookies.get(LOGIN_COOKIE))
		supplied = request.form.get('csrf_token', '')
		if not cookie_present or not hmac.compare_digest(supplied.encode(), g.csrf.encode()):
			abort(400, description='Форма устарела. Обновите страницу и повторите действие')

		origin = request.headers.get('Origin')
		if origin and origin != request.host_url.rstrip('/'):
			abort(400, description='Запрос с другого сайта отклонён')

	return None

def _set_session_cookie(response: Response, token: str) -> None:
	# No server-side deadline. Browsers may enforce their own cookie retention limit.
	"""Выдать HttpOnly-cookie без прикладного срока сессии; браузер может ограничить её хранение."""
	response.set_cookie(
		COOKIE, 
		token,
		expires=datetime(9999, 12, 31, tzinfo=timezone.utc),
		httponly=True,
		samesite='Strict',
		secure=False
	)

@APP.after_request
def response_headers(response: Response) -> Response:
	"""Добавить защитные заголовки, выдать CSRF-cookie входа и обновить cookie сессии."""
	if request.endpoint != 'static':
		response.headers['Cache-Control'] = 'no-store'
	response.headers['X-Content-Type-Options'] = 'nosniff'
	response.headers['X-Frame-Options'] = 'DENY'
	response.headers['Referrer-Policy'] = 'same-origin'
	response.headers['Content-Security-Policy'] = (
		"default-src 'self';"
		"style-src 'self';"
		"script-src 'self';"
		"img-src 'self' data:;"
		"form-action 'self';"
		"frame-ancestors 'none';"
		"base-uri 'none'"
	)

	auth, csrf = g.get('auth'), g.get('csrf')
	if request.endpoint == 'login' and response.status_code != 302 and not auth and csrf:
		response.set_cookie(LOGIN_COOKIE, csrf, httponly=True, samesite='Strict')
	
	if auth and request.endpoint != 'logout':
		_set_session_cookie(response, request.cookies[COOKIE])
	return response

# ===================
#  Web
# ====================

@APP.before_request
def select_profile() -> Response | None:
	"""Выбрать профиль сессии либо первый доступный; без профилей открыть создание."""
	if request.endpoint in ('static', 'login', 'logout') or not g.get('auth'):
		return None
	db = dbm.get()
	g.profile = db.execute(
		'SELECT * FROM profiles WHERE id = ?', (g.auth.profile_id,)
	).fetchone()

	if g.profile is None:
		g.profile = db.execute('SELECT * FROM profiles ORDER BY id LIMIT 1').fetchone()
		if g.profile:
			with db:
				db.execute(
					'UPDATE auth_sessions SET profile_id=? WHERE token_hash=?',
					(g.profile['id'], g.auth.token_hash)
				)
		elif request.endpoint != 'new_profile':
			return redirect(url_for('new_profile'))

	return None

@APP.context_processor
def context() -> dict[str, Any]:
	"""Предоставить шаблонам профили, валюты, время сервера и сообщения о результате."""
	profiles = []
	if g.get('auth'):
		profiles = dbm.get().execute(
			'SELECT * FROM profiles ORDER BY id'
		).fetchall()

	notices = {
		'saved': 'Изменения сохранены',
		'created': 'Запись добавлена',
		'deleted': 'Удалено без возможности восстановления',
		'profile': 'Профиль создан',
		'switched': 'Профиль переключён'
	}
	return dict(
		currencies=CURRENCIES,
		symbols=SYMBOLS,
		profiles=profiles,
		now_local=g.now.strftime('%Y-%m-%dT%H:%M:%S'),
		today=datetime.now().strftime('%Y-%m-%d'),
		notice=notices.get(request.args.get('done', ''))
	)

@APP.template_filter('money')
def format_money(value: int) -> str:
	"""Фильтр money: знаковые сотые в строку с запятой и разделителями тысяч."""
	sign = '−' if value < 0 else ''
	whole, cents = divmod(abs(value), 100)
	return sign + f'{whole:,}'.replace(',', '\u00a0') + f',{cents:02d}'

@APP.template_filter('amount_input')
def amount_input(value: int) -> str:
	"""Фильтр amount_input: знаковые сотые в десятичную строку для поля формы."""
	whole, cents = divmod(abs(value), 100)
	return ('-' if value < 0 else '') + f'{whole}.{cents:02d}'

@APP.template_filter('localdate')
def localdate(value: datetime) -> str:
	"""Фильтр localdate: локальные дата и время ISO в формат ДД.ММ.ГГГГ · ЧЧ:ММ."""
	return value.strftime('%d.%m.%Y · %H:%M')

@APP.template_filter('calendar_date')
def calendar_date(value: date) -> str:
	"""Фильтр calendar_date: дата ISO в формат ДД.ММ.ГГГГ."""
	return value.strftime('%d.%m.%Y')

@APP.errorhandler(400)
@APP.errorhandler(404)
@APP.errorhandler(413)
def client_error(error: HTTPException) -> tuple[str, int]:
	"""Показать общую страницу ошибок 400, 404 и превышения размера запроса 413."""
	assert error.code is not None
	messages = {
		404: 'Страница или запись не найдена',
		413: 'Слишком большой запрос'
	}

	return render_template(
		'error.html',
		code=error.code,
		message=messages.get(error.code, error.description)
	), error.code

# ===================
#  Web routers
# ====================

@APP.route('/login', methods=['GET', 'POST'])
def login() -> ResponseReturnValue:
	"""Показать вход или проверить пароль с ограничением попыток; создать сессию."""
	if g.auth:
		return redirect(url_for('overview'))

	status = 200
	if request.method == 'POST':
		db = dbm.get()
		address, now = request.remote_addr or 'local', time.time()
		# Reserve the attempt atomically before the costly password check.

		with db:
			db.execute('BEGIN IMMEDIATE')
			db.execute('DELETE FROM login_attempts WHERE window_started < ?', (now - 900,))
			attempt = db.execute(
				'SELECT * FROM login_attempts WHERE address = ?', (address,)
			).fetchone()

			if attempt and attempt['count'] >= config.get().max_login_attempts:
				g.error = 'Слишком много попыток. Попробуйте через 15 минут.'
				return render_template('login.html'), 429
			db.execute(
				'INSERT INTO login_attempts VALUES (?, 1, ?) '
				'ON CONFLICT(address) DO UPDATE SET count = count + 1',
				(address, now)
			)

		password = request.form.get('password', '')
		if (len(password) <= 512
			and check_password_hash(config.get().password_hash, password)):
			token = secrets.token_urlsafe(32)

			with db:
				db.execute('DELETE FROM login_attempts WHERE address = ?', (address,))
				db.execute(
					'INSERT INTO auth_sessions'
					'(token_hash, password_fingerprint, csrf_token)'
					'VALUES (?, ?, ?)',
					(
						config.token_hash(token),
						config.get().password_fingerprint,
						secrets.token_urlsafe(32)
					)
				)

			response = redirect(url_for('overview'))
			_set_session_cookie(response, token)
			response.delete_cookie(LOGIN_COOKIE)
			return response

		g.error, status = 'Неверный пароль', 401

	return render_template('login.html'), status

@APP.post('/logout')
def logout() -> Response:
	"""Отозвать только текущую сессию в БД и удалить её cookie из браузера."""
	with dbm.get() as db:
		db.execute('DELETE FROM auth_sessions WHERE token_hash = ?', (g.auth.token_hash,))

	response = redirect(url_for('login'))
	response.delete_cookie(COOKIE)
	return response

@APP.get('/')
def overview() -> str:
	"""Показать текущие балансы и две группы статистики за выбранный период."""
	entries = fetch_entries_for(g.profile['id'])
	period = request.args.get('period', 'month')
	currency = request.args.get('currency', 'RUB')
	if currency not in CURRENCIES:
		abort(400, description='Неизвестная валюта')

	formats = {'day': '%Y-%m-%d', 'week': '%Y-%m-%d', 'range': '%Y-%m-%d', 'month': '%Y-%m', 'year': '%Y'}
	selected = request.args.get('date') or datetime.now().strftime(
		formats.get(period, '%Y-%m')
	)
	end_selected = request.args.get('end_date', datetime.now().strftime('%Y-%m-%d'))

	try:
		stats = calc_statistics(entries, period, selected, currency, end_selected, g.now)
	except ValidationError as error:
		abort(400, description=str(error))

	return render_template(
		'overview.html',
		active='overview',
		balances=fetch_balances(g.profile['id'], entries, g.now),
		stats=stats,
		period=period,
		selected=selected,
		end_selected=end_selected,
		currency=currency
	)

def _fetch_catalog(table: Literal['categories', 'tags']) -> list[sqlite3.Row]:
	"""Получить категории или теги текущего профиля в порядке названий."""
	return dbm.get().execute(
		f'SELECT * FROM {table} WHERE profile_id = ? ORDER BY name COLLATE NOCASE',
		(g.profile['id'],)
	).fetchall()

@APP.get('/entries')
def entry_list() -> str:
	"""Отфильтровать журнал и показать текущие и будущие записи отдельными группами.
	
	Все переданные tag должны присутствовать; category_id=none означает NULL.
	Фильтр валюты учитывает обе стороны трансфера; даты включительны."""
	entries = fetch_entries_for(g.profile['id'])
	args = request.args

	if args.get('kind'):
		entries = [e for e in entries if e.kind == args['kind']]
	if args.get('category_id') == 'none':
		entries = [e for e in entries if e.category_id is None]
	elif args.get('category_id'):
		entries = [
			e for e in entries if str(e.category_id) == args['category_id']
		]
	if args.get('currency'):
		entries = [
			e for e in entries if args['currency'] in (e.currency, e.destination_currency)
		]
	selected_tags = {tag for tag in args.getlist('tag') if tag}
	q = args.get('q')
	if selected_tags:
		entries = [
			e for e in entries if selected_tags.issubset({str(t.id) for t in e.tags})
		]

	if q:
		entries = [e for e in entries if q.casefold() in e.comment.casefold()]
	
	for date_field, lower in (('from', True), ('to', False)):
		raw = args.get(date_field)
		if raw:
			try:
				boundary = datetime.combine(datetime.strptime(raw, '%Y-%m-%d').date(), day_time.min if lower else day_time.max)
			except ValueError:
				abort(400, description='Некорректная дата фильтра.')

			entries = [e for e in entries if (
				e.occurred_at >= boundary if lower else e.occurred_at <= boundary
			)]

	return render_template(
		'entries.html',
		active='entries',
		entries=entries,
		current_entries=[e for e in entries if not e.is_planned(g.now)],
		planned_entries=[e for e in entries if e.is_planned(g.now)],
		categories=_fetch_catalog('categories'),
		tags=_fetch_catalog('tags'),
		selected_tags=selected_tags,
	)

@APP.route('/entries/new', methods=['GET', 'POST'])
@APP.route('/entries/<int:entry_id>/edit', methods=['GET', 'POST'])
def entry_form(entry_id: int | None = None) -> ResponseReturnValue:
	"""Показать форму новой/существующей записи или сохранить её после проверки.
	
	При ошибке сохранить введённые значения и вернуть статус 422."""
	entry = Entry.from_row(fetch_owned('entries', entry_id, g.profile['id'])) if entry_id is not None else None
	selected_tags = [str(row['tag_id']) for row in dbm.get().execute(
		'SELECT tag_id FROM entry_tags WHERE entry_id=? AND profile_id=?',
		(entry_id, g.profile['id']),
	)] if entry is not None else []
	values: Mapping[str, str] = {}
	if entry is not None:
		values = {
			'kind': entry.kind, 'amount': amount_input(abs(entry.amount)),
			'currency': entry.currency, 'occurred_at': entry.occurred_at.isoformat(timespec='seconds'),
			'comment': entry.comment, 'category_id': str(entry.category_id) if entry.category_id is not None else '',
			'destination_currency': entry.destination_currency or 'USD',
			'destination_amount': amount_input(entry.destination_amount) if entry.destination_amount is not None else '',
		}

	status = 200
	if request.method == 'POST':
		values = request.form
		selected_tags = request.form.getlist('tags')
		try:
			save_entry(g.profile['id'], request.form, entry_id)
			return redirect(url_for('entry_list', done='saved' if entry else 'created'))
		except ValidationError as error:
			g.error, status = str(error), 422

	return render_template(
		'entry_form.html',
		active='entries',
		entry=entry,
		values=values,
		selected_tags=selected_tags,
		categories=_fetch_catalog('categories'),
		tags=_fetch_catalog('tags')
	), status

@APP.post('/entries/<int:entry_id>/delete')
def delete_entry(entry_id: int) -> Response:
	"""Необратимо удалить запись текущего профиля вместе со связями тегов."""
	fetch_owned('entries', entry_id, g.profile['id'])
	with dbm.get() as db:
		db.execute(
			'DELETE FROM entries WHERE id = ? AND profile_id = ?',
			(entry_id, g.profile['id'])
		)
	return redirect(url_for('entry_list', done='deleted'))

@APP.route('/profiles/new', methods=['GET', 'POST'])
def new_profile() -> ResponseReturnValue:
	"""Показать создание профиля; после сохранения выбрать его в текущей сессии."""
	status = 200
	if request.method == 'POST':
		try:
			profile_id = create_profile(
				request.form.get('name', ''), request.form
			)

			with dbm.get() as db:
				db.execute(
					'UPDATE auth_sessions SET profile_id = ? WHERE token_hash = ?',
					(profile_id, g.auth.token_hash)
				)
			return redirect(url_for('overview', done='profile'))
		except ValidationError as error:
			g.error, status = str(error), 422

	return render_template(
		'profile_form.html',
		active='settings',
		creating=True,
		values=request.form
	), status

@APP.post('/profiles/switch')
def switch_profile() -> Response:
	"""Выбрать существующий профиль для текущей сессии без отдельного пароля."""
	profile = dbm.get().execute(
		'SELECT * FROM profiles WHERE id = ?', (request.form.get('profile_id'),)
	).fetchone()

	if profile is None:
		abort(404)

	with dbm.get() as db:
		db.execute(
			'UPDATE auth_sessions SET profile_id = ? WHERE token_hash = ?',
			(profile['id'], g.auth.token_hash)
		)
	return redirect(url_for('overview', done='switched'))

@APP.route('/settings', methods=['GET', 'POST'])
def settings() -> ResponseReturnValue:
	"""Показать или изменить название и начальный капитал текущего профиля."""
	values: Mapping[str, str] = {
		'name': g.profile['name'],
		**{r['currency']: amount_input(r['amount']) for r in dbm.get().execute(
			'SELECT * FROM capitals WHERE profile_id=?', (g.profile['id'],)
		)}
	}

	status = 200
	if request.method == 'POST':
		values = request.form
		try:
			save_profile(
				g.profile['id'],
				request.form.get('name', ''),
				request.form
			)
			return redirect(url_for('settings', done='saved'))
		except ValidationError as error:
			g.error, status = str(error), 422

	return render_template(
		'profile_form.html',
		active='settings',
		creating=False,
		values=values
	), status

@APP.post('/profiles/delete')
def delete_profile() -> Response:
	"""Необратимо удалить профиль с журналом и справочниками; выбрать другой при следующем запросе."""
	with dbm.get() as db:
		# Delete entries before their required categories, regardless of FK cascade order.
		db.execute('DELETE FROM entries WHERE profile_id=?', (g.profile['id'],))
		db.execute('DELETE FROM profiles WHERE id=?', (g.profile['id'],))
	return redirect(url_for('overview', done='deleted'))

@APP.get('/library')
def library() -> str:
	"""Показать категории и теги текущего профиля; категория по умолчанию виртуальная."""
	return render_template(
		'library.html',
		active='library',
		categories=_fetch_catalog('categories'),
		tags=_fetch_catalog('tags')
	)

@APP.post('/library/<kind>/<action>')
def change_library(kind: str, action: str) -> ResponseReturnValue:
	"""Добавить, переименовать или удалить элемент справочника текущего профиля.
	
	Имя «Без категории» зарезервировано; удаление категории сохраняет её записи."""
	if kind not in ('categories', 'tags') or action not in ('add', 'rename', 'delete'):
		abort(404)

	db, profile_id = dbm.get(), g.profile['id']
	try:
		row = None
		if action != 'add':
			row = fetch_owned(kind, request.form.get('id'), profile_id)
		
		if (kind == 'categories' 
			and action != 'delete'
			and request.form.get('name', '').strip() == 'Без категории'):
			raise ValidationError('Название «Без категории» зарезервировано')
		elif action == 'delete' and kind == 'categories':
			assert row is not None
			delete_category(row['id'], profile_id)
		else:
			with db:
				if action == 'add':
					db.execute(
						f'INSERT INTO {kind}(profile_id, name) VALUES (?, ?)',
						(profile_id, to_name(request.form.get('name', '')))
					)
				elif action == 'rename':
					assert row is not None
					db.execute(
						f'UPDATE {kind} SET name = ? WHERE id = ? AND profile_id = ?',
						(to_name(request.form.get('name', '')), row['id'], profile_id)
					)
				else:
					assert row is not None
					db.execute(
						'DELETE FROM tags WHERE id = ? AND profile_id = ?',
						(row['id'], profile_id)
					)

		return redirect(url_for(
			'library', done=('deleted' if action == 'delete' else 'saved')
		))
	except (ValidationError, sqlite3.IntegrityError) as error:
		g.error = 'Такое название уже существует'
		if not isinstance(error, sqlite3.IntegrityError):
			g.error = str(error)

		return render_template(
			'library.html',
			active='library',
			categories=_fetch_catalog('categories'),
			tags=_fetch_catalog('tags')
		), 422

# ====================

def init(settings: config.AppConfig, *, testing: bool = False) -> Flask:
	"""Настроить и вернуть единственный APP процесса, не создавая новый Flask.

	Маршруты и обработчики зарегистрированы один раз при импорте модуля.
	Повторный вызов заменяет настройки существующего APP, что нужно тестам.
	Схема и данные сохраняются, автоматической миграции нет.
	"""
	APP.extensions[config.CONFIG_KEY] = settings
	APP.config['TESTING'] = testing
	with APP.app_context():
		dbm.initialize()
		with dbm.get() as connection:
			connection.execute(
				'DELETE FROM auth_sessions WHERE password_fingerprint != ?',
				(settings.password_fingerprint,),
			)
	return APP
