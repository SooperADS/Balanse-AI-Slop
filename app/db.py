"""Одно соединение SQLite на контекст Flask с включёнными внешними ключами."""

import sqlite3

from typing import cast
from flask import current_app, g

def get() -> sqlite3.Connection:
	"""Вернуть соединение текущего контекста, создав его при первом обращении.
	
	Строки доступны по именам столбцов; запись фиксируется вызывающим кодом
	через контекстный менеджер соединения. Соединение хранится в flask.g."""
	if 'db' not in g:
		db = sqlite3.connect(current_app.config['DATABASE'], timeout=10)
		db.row_factory = sqlite3.Row
		db.execute('PRAGMA foreign_keys = ON')
		g.db = db
	return cast(sqlite3.Connection, g.db)

def close(_error: BaseException | None = None) -> None:
	"""Закрыть соединение при завершении контекста, если оно было открыто.
	
	_error передаётся Flask как причина завершения и на закрытие не влияет."""
	db: sqlite3.Connection | None = g.pop('db', None)
	if db is not None:
		db.close()
