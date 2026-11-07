"""Соединение SQLite на контекст Flask и создание отсутствующих таблиц."""

import sqlite3
from pathlib import Path
from typing import cast

from flask import g

from . import config

def get() -> sqlite3.Connection:
	"""Переиспользовать соединение запроса с Row и включёнными внешними ключами.

	Изменения фиксирует вызывающий код через контекстный менеджер соединения.
	"""
	connection = cast(sqlite3.Connection | None, g.get('db'))
	if connection is None:
		connection = sqlite3.connect(config.get().database, timeout=10)
		connection.row_factory = sqlite3.Row
		connection.execute('PRAGMA foreign_keys = ON')
		g.db = connection
	return connection

def initialize() -> None:
	"""Создать каталог и отсутствующие таблицы; существующие данные сохраняются."""
	config.get().database.parent.mkdir(parents=True, exist_ok=True)
	get().executescript(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))

def close(_error: BaseException | None = None) -> None:
	"""Закрыть соединение при выходе из контекста; _error передаётся Flask."""
	connection = cast(sqlite3.Connection | None, g.pop('db', None))
	if connection is not None:
		connection.close()
