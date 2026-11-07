"""Приложение Flask: обработчики страниц, общий вход, CSRF и контекст шаблонов."""

import sqlite3
import secrets
import segno
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
	"""Хеш токена, CSRF-токен и выбранный профиль проверенной сессии запроса."""

	token_hash: str
	csrf_token: str
	profile_id: int | None

def _find_session(token: str) -> _Session | None:
	"""Найти сессию, действующую для текущего пароля приложения.

	Args:
	    token: Исходный секрет из cookie браузера.
	Returns:
	    Проверенная сессия либо None, если она отсутствует или пароль сменён.
	Raises:
	    sqlite3.Error: Не удалось прочитать сессии."""
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
	"""Зафиксировать время запроса, проверить сессию и CSRF для POST.

	Без сессии доступны вход, приглашения и статические ресурсы.
	Raises:
	    BadRequest: POST не прошёл проверку CSRF или Origin.
	    sqlite3.Error: Не удалось прочитать сессию."""
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
		if request.endpoint not in ('login', 'invite'):
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
	"""Выдать HttpOnly-cookie без прикладного срока сессии.

	SameSite=Lax допускает вход по внешней ссылке и последующее GET-перенаправление.
	Args:
	    response: Ответ, в который добавляется cookie.
	    token: Секрет серверной сессии. Браузер может ограничить хранение cookie."""
	response.set_cookie(
		COOKIE, 
		token,
		expires=datetime(9999, 12, 31, tzinfo=timezone.utc),
		httponly=True,
		samesite='Lax',
		secure=False
	)

@APP.after_request
def response_headers(response: Response) -> Response:
	"""Добавить защитные заголовки и обновить cookie входа/сессии.

	Args:
	    response: Ответ Flask, изменяемый перед отправкой.
	Returns:
	    Тот же ответ с заголовками и cookie."""
	if request.endpoint != 'static':
		response.headers['Cache-Control'] = 'no-store'
	response.headers['X-Content-Type-Options'] = 'nosniff'
	response.headers['X-Frame-Options'] = 'DENY'
	# Не передаём путь и пароль приглашения в Referer, сохраняя Origin обычных POST-форм.
	response.headers['Referrer-Policy'] = 'strict-origin'
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
	if request.endpoint in ('login', 'invite') and response.status_code != 302 and not auth and csrf:
		response.set_cookie(LOGIN_COOKIE, csrf, httponly=True, samesite='Strict')
	
	if auth and request.endpoint != 'logout':
		_set_session_cookie(response, request.cookies[COOKIE])
	return response

# ===================
#  Web
# ====================

@APP.before_request
def select_profile() -> Response | None:
	"""Выбрать профиль сессии или первый доступный; иначе открыть создание.

	Raises:
	    sqlite3.Error: Не удалось прочитать профили или обновить сессию."""
	if request.endpoint in ('static', 'login', 'invite', 'logout') or not g.get('auth'):
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
	"""Подготовить общий контекст всех Jinja-страниц.

	Returns:
	    Профили, валюты, время сервера и сообщение о результате действия.
	Raises:
	    sqlite3.Error: Не удалось прочитать список профилей."""
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
	"""Отформатировать сумму для отображения в интерфейсе.

	Args:
	    value: Знаковая сумма в целых копейках/центах.
	Returns:
	    Строка с запятой и неразрывными разделителями тысяч."""
	sign = '−' if value < 0 else ''
	whole, cents = divmod(abs(value), 100)
	return sign + f'{whole:,}'.replace(',', '\u00a0') + f',{cents:02d}'

@APP.template_filter('amount_input')
def amount_input(value: int) -> str:
	"""Отформатировать сумму для заполнения поля формы.

	Args:
	    value: Знаковая сумма в целых копейках/центах.
	Returns:
	    Десятичная строка с точкой и двумя дробными знаками."""
	whole, cents = divmod(abs(value), 100)
	return ('-' if value < 0 else '') + f'{whole}.{cents:02d}'

@APP.template_filter('localdate')
def localdate(value: datetime) -> str:
	"""Отформатировать локальные дату и время записи.

	Args:
	    value: Время операции в часовом поясе сервера.
	Returns:
	    Строка ДД.ММ.ГГГГ · ЧЧ:ММ."""
	return value.strftime('%d.%m.%Y · %H:%M')

@APP.template_filter('calendar_date')
def calendar_date(value: date) -> str:
	"""Отформатировать календарную дату статистики.

	Args:
	    value: Дата включительной границы периода.
	Returns:
	    Строка ДД.ММ.ГГГГ."""
	return value.strftime('%d.%m.%Y')

@APP.errorhandler(400)
@APP.errorhandler(404)
@APP.errorhandler(413)
def client_error(error: HTTPException) -> tuple[str, int]:
	"""Показать общую страницу ошибок 400, 404 и 413.

	Args:
	    error: HTTP-исключение Flask с заданным кодом."""
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

def _check_password(password: str) -> Literal[200, 401, 429]:
	"""Проверить пароль с общим для формы и приглашений лимитом по IP.

	Args:
	    password: Пароль из формы или ссылки; пустая строка разрешена.
	Returns:
	    200 при успехе, 401 при неверном пароле, 429 при исчерпанном лимите.
	    При отказе g.error содержит сообщение для страницы входа.
	Raises:
	    sqlite3.Error: Не удалось зарезервировать или сбросить попытку.
	"""
	db = dbm.get()
	address, now = request.remote_addr or 'local', time.time()
	# Резервируем попытку до дорогостоящей проверки хеша, исключая гонку запросов.
	with db:
		db.execute('BEGIN IMMEDIATE')
		db.execute('DELETE FROM login_attempts WHERE window_started < ?', (now - 900,))
		attempt = db.execute(
			'SELECT count FROM login_attempts WHERE address = ?', (address,),
		).fetchone()
		if attempt and attempt['count'] >= config.get().max_login_attempts:
			g.error = 'Слишком много попыток. Попробуйте через 15 минут.'
			return 429
		db.execute(
			'INSERT INTO login_attempts VALUES (?, 1, ?) '
			'ON CONFLICT(address) DO UPDATE SET count = count + 1',
			(address, now),
		)
	if len(password) > 512 or not check_password_hash(config.get().password_hash, password):
		g.error = 'Неверный пароль'
		return 401
	with db:
		db.execute('DELETE FROM login_attempts WHERE address = ?', (address,))
	return 200

def _authorize(profile_id: int | None = None) -> Response:
	"""Создать сессию или переключить профиль существующей после проверки пароля.

	Args:
	    profile_id: Проверенный ID приглашения; None выбирает профиль при следующем запросе.
	Returns:
	    Перенаправление на обзор с cookie сессии и без пароля в адресе.
	Raises:
	    sqlite3.Error: Не удалось сохранить сессию или выбранный профиль.
	"""
	with dbm.get() as db:
		if g.auth:
			token = request.cookies[COOKIE]
			db.execute(
				'UPDATE auth_sessions SET profile_id=? WHERE token_hash=?',
				(profile_id, g.auth.token_hash),
			)
		else:
			token = secrets.token_urlsafe(32)
			db.execute(
				'INSERT INTO auth_sessions '
				'(token_hash, password_fingerprint, csrf_token, profile_id) VALUES (?, ?, ?, ?)',
				(config.token_hash(token), config.get().password_fingerprint,
				 secrets.token_urlsafe(32), profile_id),
			)

	response = redirect(url_for('overview'))
	_set_session_cookie(response, token)
	response.delete_cookie(LOGIN_COOKIE)
	return response

@APP.route('/login', methods=['GET', 'POST'])
def login() -> ResponseReturnValue:
	"""Показать вход или проверить пароль и создать сессию.

	Ошибочный пароль даёт 401, превышение лимита попыток по IP — 429.
	Raises:
	    sqlite3.Error: Не удалось учесть попытку или сохранить сессию.
	"""
	if g.auth:
		return redirect(url_for('overview'))
	status = 200
	if request.method == 'POST':
		status: int = _check_password(request.form.get('password', ''))
		if status == 200:
			return _authorize()
	return render_template('login.html'), status

@APP.get('/invite')
def invite() -> ResponseReturnValue:
	"""Проверить пароль ссылки и открыть указанный профиль.

	Параметры запроса: profile_id и password; пустой пароль передаётся явно.
	Ошибочный пароль даёт 401, исчерпанный общий лимит попыток — 429.
	Raises:
	    BadRequest: Не передан пароль или неверно записан ID профиля.
	    NotFound: Профиль удалён или не существует.
	    sqlite3.Error: Не удалось проверить доступ или сохранить сессию.
	"""
	profile_id = request.args.get('profile_id', type=int)
	if ((profile_id and not 0 < profile_id <= 9223372036854775807) 
		or 'password' not in request.args):
		abort(400, description='В ссылке отсутствует пароль или указан неверный профиль')

	status = _check_password(request.args['password'])
	if status != 200:
		return render_template('login.html'), status
	elif profile_id and dbm.get().execute(
		'SELECT id FROM profiles WHERE id = ?', (profile_id,)
	).fetchone() is None:
		abort(404)

	return _authorize(profile_id)

@APP.get('/profiles/share')
def share_profile() -> str:
	"""Показать текстовую ссылку и локальный QR-код текущего профиля.

	Без server.public_node показать подсказку настройки; слишком длинная ссылка
	остаётся доступна текстом, если превышена стандартная вместимость QR-кода.
	Raises:
	    sqlite3.Error: Не удалось определить профиль при подготовке запроса.
	"""
	settings = config.get()
	link, qr_image, qr_error = '', '', ''
	if settings.public_node:
		assert settings.public_link_base
		link = settings.get_invite_link(g.profile["id"])

		try:
			qr_image = segno.make_qr(link, error='L').svg_data_uri(
				scale=4, light='white', dark='black' #'#7b8b29'
			)
		except segno.DataOverflowError:
			qr_error = (
				'Ссылка слишком длинная для QR-кода. '
				'Скопируйте её текстом или сократите пароль в конфигурации.'
			)

	return render_template(
		'share_profile.html', active='settings', invite_link=link,
		qr_image=qr_image, qr_error=qr_error,
	)

@APP.post('/logout')
def logout() -> Response:
	"""Отозвать текущую сессию и удалить её cookie из браузера.

	Raises:
	    sqlite3.Error: Не удалось удалить сессию."""
	with dbm.get() as db:
		db.execute('DELETE FROM auth_sessions WHERE token_hash = ?', (g.auth.token_hash,))

	response = redirect(url_for('login'))
	response.delete_cookie(COOKIE)
	return response

@APP.get('/')
def overview() -> str:
	"""Показать балансы и текущую/плановую статистику выбранного периода.

	Raises:
	    BadRequest: Неизвестная валюта или некорректный период.
	    sqlite3.Error: Не удалось прочитать данные профиля."""
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
	"""Прочитать справочник текущего профиля.

	Args:
	    table: categories или tags; имя берётся только из кода приложения.
	Returns:
	    Строки справочника в порядке названий без учёта регистра ASCII.
	Raises:
	    sqlite3.Error: Не удалось прочитать справочник."""
	return dbm.get().execute(
		f'SELECT * FROM {table} WHERE profile_id = ? ORDER BY name COLLATE NOCASE',
		(g.profile['id'],)
	).fetchall()

@APP.get('/entries')
def entry_list() -> str:
	"""Показать отфильтрованный журнал с отдельной группой будущих записей.

	Фильтр требует все выбранные теги; category_id=none выбирает NULL.
	Валюта учитывает обе стороны трансфера, даты включительны.
	Raises:
	    BadRequest: Неверная дата фильтра.
	    sqlite3.Error: Не удалось прочитать журнал или справочники."""
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
	"""Показать или сохранить операцию; при ошибке ввода оставить форму с 422.

	Args:
	    entry_id: ID редактируемой записи; None открывает создание.
	Raises:
	    NotFound: Запись, категория или тег отсутствует в текущем профиле.
	    sqlite3.Error: Не удалось прочитать или сохранить данные."""
	entry = None
	if entry_id:
		entry = Entry.from_row(
			fetch_owned('entries', entry_id, g.profile['id'])
		)

	selected_tags = []
	if entry:
		selected_tags = [
			str(row['tag_id']) for row in dbm.get().execute(
				'SELECT tag_id FROM entry_tags WHERE entry_id=? AND profile_id=?',
			(entry_id, g.profile['id']),
		)]

	values: Mapping[str, str] = {}
	if entry is not None:
		values = {
			'kind': entry.kind,
			'amount': amount_input(abs(entry.amount)),
			'currency': entry.currency,
			'occurred_at': entry.occurred_at.isoformat(timespec='seconds'),
			'comment': entry.comment,
			'category_id': str(entry.category_id) if entry.category_id is not None else '',
			'destination_currency': entry.destination_currency or 'USD',
			'destination_amount': (
				amount_input(entry.destination_amount)
				if entry.destination_amount is not None else ''
			),
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
	"""Необратимо удалить операцию вместе со связями тегов.

	Args:
	    entry_id: ID записи текущего профиля.
	Raises:
	    NotFound: Запись отсутствует в профиле.
	    sqlite3.Error: Не удалось удалить запись."""

	with dbm.get() as db:
		if db.execute(
			'DELETE FROM entries WHERE id = ? AND profile_id = ?',
			(entry_id, g.profile['id'])
		).rowcount == 0:
			abort(404)
	return redirect(url_for('entry_list', done='deleted'))

@APP.route('/profiles/new', methods=['GET', 'POST'])
def new_profile() -> ResponseReturnValue:
	"""Создать профиль и выбрать его; ошибки полей показать со статусом 422.

	Raises:
	    sqlite3.Error: Не удалось создать профиль или обновить сессию."""
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
	"""Выбрать существующий профиль для текущей сессии.

	Raises:
	    NotFound: Переданный profile_id не соответствует профилю.
	    sqlite3.Error: Не удалось прочитать профиль или обновить сессию."""
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
	"""Изменить название и начальный капитал; ошибки полей показать с 422.

	Raises:
	    sqlite3.Error: Не удалось прочитать или сохранить профиль."""
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
	"""Необратимо удалить текущий профиль с журналом и справочниками.

	Raises:
	    sqlite3.Error: Не удалось удалить данные профиля."""
	with dbm.get() as db:
		# Сначала удаляем операции, чтобы их ссылки не мешали каскадному удалению категорий.
		db.execute('DELETE FROM entries WHERE profile_id=?', (g.profile['id'],))
		db.execute('DELETE FROM profiles WHERE id=?', (g.profile['id'],))
	return redirect(url_for('overview', done='deleted'))

@APP.get('/library')
def library() -> str:
	"""Показать категории и теги; «Без категории» отображается без строки БД.

	Raises:
	    sqlite3.Error: Не удалось прочитать справочники."""
	return render_template(
		'library.html',
		active='library',
		categories=_fetch_catalog('categories'),
		tags=_fetch_catalog('tags')
	)

@APP.post('/library/<kind>/<action>')
def change_library(kind: str, action: str) -> ResponseReturnValue:
	"""Изменить справочник; неверное или повторное название показать с 422.

	Args:
	    kind: categories или tags.
	    action: add, rename или delete; категория при удалении сохраняет записи.
	Raises:
	    NotFound: Неизвестный справочник, действие или ID внутри профиля.
	    sqlite3.Error: Ошибка БД, кроме обработанного конфликта ограничений."""
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
	"""Настроить единственный APP процесса и создать отсутствующие таблицы.

	Повторная настройка допустима до обслуживания запросов и в последовательных
	тестах. Экземпляр Flask, маршруты, схема и имеющиеся данные сохраняются.
	Args:
	    settings: Проверенные настройки БД и входа.
	    testing: Включить режим TESTING Flask.
	Returns:
	    Единственный экземпляр APP с применёнными настройками.
	Raises:
	    OSError: Каталог БД или файл схемы недоступен.
	    sqlite3.Error: Не удалось подготовить БД или отозвать старые сессии."""
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
