import hashlib
import hmac
import secrets
import time
from datetime import datetime, timezone

from flask import abort, current_app, g, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash

from .db import get_db

COOKIE = 'budget_session'
LOGIN_COOKIE = 'budget_login_csrf'

def token_hash(value):
	return hashlib.sha256(value.encode()).hexdigest()

def install_auth(app):
	@app.before_request
	def authenticate():
		g.auth = None
		g.profile = None
		g.error = None
		if request.endpoint == 'static':
			return None
		raw_token = request.cookies.get(COOKIE, '')
		if raw_token:
			g.auth = get_db().execute(
				'SELECT * FROM auth_sessions WHERE token_hash=? AND password_fingerprint=?',
				(token_hash(raw_token), current_app.config['PASSWORD_FINGERPRINT']),
			).fetchone()
		if g.auth is None:
			if request.endpoint != 'login':
				return redirect(url_for('login'))
			g.csrf = request.cookies.get(LOGIN_COOKIE) or secrets.token_urlsafe(32)
		else:
			g.csrf = g.auth['csrf_token']
		if request.method == 'POST':
			cookie_present = g.auth is not None or bool(request.cookies.get(LOGIN_COOKIE))
			supplied = request.form.get('csrf_token', '')
			if not cookie_present or not hmac.compare_digest(supplied.encode(), g.csrf.encode()):
				abort(400, description='Форма устарела. Обновите страницу и повторите действие.')
			origin = request.headers.get('Origin')
			if origin and origin != request.host_url.rstrip('/'):
				abort(400, description='Запрос с другого сайта отклонён.')

	@app.after_request
	def response_headers(response):
		if request.endpoint != 'static':
			response.headers['Cache-Control'] = 'no-store'
		response.headers['X-Content-Type-Options'] = 'nosniff'
		response.headers['X-Frame-Options'] = 'DENY'
		response.headers['Referrer-Policy'] = 'same-origin'
		response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
		if request.endpoint == 'login' and response.status_code != 302 and not g.get('auth') and g.get('csrf'):
			response.set_cookie(LOGIN_COOKIE, g.csrf, httponly=True, samesite='Strict')
		if g.get('auth') and request.endpoint != 'logout':
			set_session_cookie(response, request.cookies[COOKIE])
		return response

	@app.route('/login', methods=['GET', 'POST'])
	def login():
		if g.auth:
			return redirect(url_for('overview'))
		if request.method == 'POST':
			db = get_db()
			address, now = request.remote_addr or 'local', time.time()
			# Reserve the attempt atomically before the costly password check.
			with db:
				db.execute('BEGIN IMMEDIATE')
				db.execute('DELETE FROM login_attempts WHERE window_started < ?', (now - 900,))
				attempt = db.execute('SELECT * FROM login_attempts WHERE address=?', (address,)).fetchone()
				if attempt and attempt['count'] >= 5:
					g.error = 'Слишком много попыток. Попробуйте через 15 минут.'
					return render_template('login.html'), 429
				db.execute('INSERT INTO login_attempts VALUES (?, 1, ?) ON CONFLICT(address) DO UPDATE SET count=count+1', (address, now))
			password = request.form.get('password', '')
			if len(password) <= 4096 and check_password_hash(current_app.config['PASSWORD_HASH'], password):
				token = secrets.token_urlsafe(32)
				with db:
					db.execute('DELETE FROM login_attempts WHERE address=?', (address,))
					db.execute('INSERT INTO auth_sessions(token_hash, password_fingerprint, csrf_token) VALUES (?, ?, ?)',
							(token_hash(token), current_app.config['PASSWORD_FINGERPRINT'], secrets.token_urlsafe(32)))
				response = redirect(url_for('overview'))
				set_session_cookie(response, token)
				response.delete_cookie(LOGIN_COOKIE)
				return response
			g.error = 'Неверный пароль.'
			return render_template('login.html'), 401
		return render_template('login.html')

	@app.post('/logout')
	def logout():
		with get_db() as db:
			db.execute('DELETE FROM auth_sessions WHERE token_hash=?', (g.auth['token_hash'],))
		response = redirect(url_for('login'))
		response.delete_cookie(COOKIE)
		return response

def set_session_cookie(response, token):
	# No server-side deadline. Browsers may enforce their own cookie retention limit.
	response.set_cookie(COOKIE, token, expires=datetime(9999, 12, 31, tzinfo=timezone.utc),
						httponly=True, samesite='Strict', secure=False)
