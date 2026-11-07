from datetime import datetime
from pathlib import Path
import sqlite3

from flask import Flask, abort, g, redirect, render_template, request, url_for

from .auth import install_auth, token_hash
from .db import close_db, get_db, init_db
from .domain import (
	CURRENCIES,
	SYMBOLS,
	ValidationError,
	balances,
	create_profile,
	delete_category,
	entries_for, 
	name, 
	owned, 
	save_entry,
	save_profile,
	statistics
)

def create_app(config):
	app = Flask(__name__)
	app.config.update(MAX_CONTENT_LENGTH=128 * 1024, DATABASE=str(Path('data/budget.sqlite3').resolve()))
	app.config.update(config)
	app.config.setdefault('PASSWORD_FINGERPRINT', token_hash(app.config['PASSWORD_HASH']))
	app.teardown_appcontext(close_db)
	with app.app_context():
		init_db()
		with get_db() as db:
			db.execute('DELETE FROM auth_sessions WHERE password_fingerprint != ?', (app.config['PASSWORD_FINGERPRINT'],))
	install_auth(app)

	@app.before_request
	def select_profile():
		if request.endpoint in ('static', 'login', 'logout') or not g.get('auth'):
			return None
		db = get_db()
		g.profile = db.execute('SELECT * FROM profiles WHERE id=?', (g.auth['profile_id'],)).fetchone()
		if g.profile is None:
			g.profile = db.execute('SELECT * FROM profiles ORDER BY id LIMIT 1').fetchone()
			if g.profile:
				with db:
					db.execute('UPDATE auth_sessions SET profile_id=? WHERE token_hash=?', (g.profile['id'], g.auth['token_hash']))
		if g.profile is None and request.endpoint != 'new_profile':
			return redirect(url_for('new_profile'))

	def catalog(table):
		return get_db().execute(f'SELECT * FROM {table} WHERE profile_id=? ORDER BY ' +
							('is_default DESC, ' if table == 'categories' else '') + 'name COLLATE NOCASE', (g.profile['id'],)).fetchall()

	@app.context_processor
	def context():
		profiles = get_db().execute('SELECT * FROM profiles ORDER BY id').fetchall() if g.get('auth') else []
		notices = {'saved': 'Изменения сохранены.', 'created': 'Запись добавлена.', 'deleted': 'Удалено без возможности восстановления.',
				'profile': 'Профиль создан.', 'switched': 'Профиль переключён.'}
		return dict(currencies=CURRENCIES, symbols=SYMBOLS, profiles=profiles,
					now_local=datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
					today=datetime.now().strftime('%Y-%m-%d'), notice=notices.get(request.args.get('done')))

	@app.template_filter('money')
	def format_money(value):
		sign = '−' if value < 0 else ''
		whole, cents = divmod(abs(value), 100)
		return sign + f'{whole:,}'.replace(',', '\u00a0') + f',{cents:02d}'

	@app.template_filter('amount_input')
	def amount_input(value):
		whole, cents = divmod(abs(value), 100)
		return ('-' if value < 0 else '') + f'{whole}.{cents:02d}'

	@app.template_filter('localdate')
	def localdate(value):
		return datetime.fromisoformat(value).strftime('%d.%m.%Y · %H:%M')

	@app.get('/')
	def overview():
		entries = entries_for(g.profile['id'])
		period = request.args.get('period', 'month')
		currency = request.args.get('currency', 'RUB')
		if currency not in CURRENCIES:
			abort(400, description='Неизвестная валюта.')
		formats = {'day': '%Y-%m-%d', 'month': '%Y-%m', 'year': '%Y'}
		selected = request.args.get('date') or datetime.now().strftime(formats.get(period, '%Y-%m'))
		try:
			stats = statistics(entries, period, selected, currency)
		except ValidationError as error:
			abort(400, description=str(error))
		return render_template('overview.html', active='overview', balances=balances(g.profile['id'], entries),
							stats=stats, period=period, selected=selected, currency=currency)

	@app.get('/entries')
	def entry_list():
		entries = entries_for(g.profile['id'])
		args = request.args
		for field in ('currency', 'kind', 'category_id'):
			if args.get(field):
				entries = [e for e in entries if str(e[field]) == args[field]]
		if args.get('tag'):
			entries = [e for e in entries if any(str(t['id']) == args['tag'] for t in e['tags'])]
		if args.get('q'):
			entries = [e for e in entries if args['q'].casefold() in e['comment'].casefold()]
		for field, lower in (('from', True), ('to', False)):
			raw = args.get(field)
			if raw:
				try:
					datetime.strptime(raw, '%Y-%m-%d')
				except ValueError:
					abort(400, description='Некорректная дата фильтра.')
				entries = [e for e in entries if (e['occurred_at'][:10] >= raw if lower else e['occurred_at'][:10] <= raw)]
		return render_template('entries.html', active='entries', entries=entries, categories=catalog('categories'), tags=catalog('tags'))

	@app.route('/entries/new', methods=['GET', 'POST'])
	@app.route('/entries/<int:entry_id>/edit', methods=['GET', 'POST'])
	def entry_form(entry_id=None):
		entry = dict(owned('entries', entry_id, g.profile['id'])) if entry_id is not None else None
		selected_tags = [str(r['tag_id']) for r in get_db().execute('SELECT tag_id FROM entry_tags WHERE entry_id=? AND profile_id=?', (entry_id, g.profile['id']))] if entry else []
		values = dict(entry or {})
		if entry:
			values['amount'] = amount_input(entry['amount'])
		status = 200
		if request.method == 'POST':
			values = request.form
			selected_tags = request.form.getlist('tags')
			try:
				save_entry(g.profile['id'], request.form, entry_id)
				return redirect(url_for('entry_list', done='saved' if entry else 'created'))
			except ValidationError as error:
				g.error, status = str(error), 422
		return render_template('entry_form.html', active='entries', entry=entry, values=values,
							selected_tags=selected_tags, categories=catalog('categories'), tags=catalog('tags')), status

	@app.post('/entries/<int:entry_id>/delete')
	def delete_entry(entry_id):
		owned('entries', entry_id, g.profile['id'])
		with get_db() as db:
			db.execute('DELETE FROM entries WHERE id=? AND profile_id=?', (entry_id, g.profile['id']))
		return redirect(url_for('entry_list', done='deleted'))

	@app.route('/profiles/new', methods=['GET', 'POST'])
	def new_profile():
		status = 200
		if request.method == 'POST':
			try:
				profile_id = create_profile(request.form.get('name', ''), request.form)
				with get_db() as db:
					db.execute('UPDATE auth_sessions SET profile_id=? WHERE token_hash=?', (profile_id, g.auth['token_hash']))
				return redirect(url_for('overview', done='profile'))
			except ValidationError as error:
				g.error, status = str(error), 422
		return render_template('profile_form.html', active='settings', creating=True, values=request.form), status

	@app.post('/profiles/switch')
	def switch_profile():
		profile = get_db().execute('SELECT * FROM profiles WHERE id=?', (request.form.get('profile_id'),)).fetchone()
		if profile is None:
			abort(404)
		with get_db() as db:
			db.execute('UPDATE auth_sessions SET profile_id=? WHERE token_hash=?', (profile['id'], g.auth['token_hash']))
		return redirect(url_for('overview', done='switched'))

	@app.route('/settings', methods=['GET', 'POST'])
	def settings():
		values = {'name': g.profile['name'], **{r['currency']: amount_input(r['amount']) for r in get_db().execute('SELECT * FROM capitals WHERE profile_id=?', (g.profile['id'],))}}
		status = 200
		if request.method == 'POST':
			values = request.form
			try:
				save_profile(g.profile['id'], request.form.get('name', ''), request.form)
				return redirect(url_for('settings', done='saved'))
			except ValidationError as error:
				g.error, status = str(error), 422
		return render_template('profile_form.html', active='settings', creating=False, values=values), status

	@app.post('/profiles/delete')
	def delete_profile():
		with get_db() as db:
			# Delete entries before their required categories, regardless of FK cascade order.
			db.execute('DELETE FROM entries WHERE profile_id=?', (g.profile['id'],))
			db.execute('DELETE FROM profiles WHERE id=?', (g.profile['id'],))
		return redirect(url_for('overview', done='deleted'))

	@app.get('/library')
	def library():
		return render_template('library.html', active='library', categories=catalog('categories'), tags=catalog('tags'))

	@app.post('/library/<kind>/<action>')
	def change_library(kind, action):
		if kind not in ('categories', 'tags') or action not in ('add', 'rename', 'delete'):
			abort(404)
		db, profile_id = get_db(), g.profile['id']
		try:
			row = None
			if action != 'add':
				row = owned(kind, request.form.get('id'), profile_id)
				if kind == 'categories' and row['is_default']:
					raise ValidationError('Категорию «Без категории» нельзя изменить или удалить.')
			if action == 'delete' and kind == 'categories':
				delete_category(row['id'], profile_id)
			else:
				with db:
					if action == 'add':
						db.execute(f'INSERT INTO {kind}(profile_id, name) VALUES (?, ?)', (profile_id, name(request.form.get('name', ''))))
					elif action == 'rename':
						db.execute(f'UPDATE {kind} SET name=? WHERE id=? AND profile_id=?', (name(request.form.get('name', '')), row['id'], profile_id))
					else:
						db.execute('DELETE FROM tags WHERE id=? AND profile_id=?', (row['id'], profile_id))
			return redirect(url_for('library', done='deleted' if action == 'delete' else 'saved'))
		except (ValidationError, sqlite3.IntegrityError) as error:
			g.error = 'Такое название уже существует.' if isinstance(error, sqlite3.IntegrityError) else str(error)
			return render_template('library.html', active='library', categories=catalog('categories'), tags=catalog('tags')), 422

	@app.errorhandler(400)
	@app.errorhandler(404)
	@app.errorhandler(413)
	def client_error(error):
		messages = {404: 'Страница или запись не найдена.', 413: 'Слишком большой запрос.'}
		return render_template('error.html', code=error.code, message=messages.get(error.code, error.description)), error.code

	return app
