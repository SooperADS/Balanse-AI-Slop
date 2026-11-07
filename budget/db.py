from pathlib import Path
import sqlite3

from flask import current_app, g

def get_db():
	if 'db' not in g:
		db = sqlite3.connect(current_app.config['DATABASE'], timeout=10)
		db.row_factory = sqlite3.Row
		db.execute('PRAGMA foreign_keys = ON')
		g.db = db
	return g.db

def init_db():
	path = Path(current_app.config['DATABASE'])
	path.parent.mkdir(parents=True, exist_ok=True)
	get_db().executescript(Path(__file__).with_name('schema.sql').read_text())

def close_db(_error=None):
	db = g.pop('db', None)
	if db is not None:
		db.close()
