"""Соединение SQLite на контекст Flask и создание отсутствующих таблиц."""

import sqlite3
from pathlib import Path
from typing import cast

from flask import g

from . import config

def get() -> sqlite3.Connection:
	"""Открыть или переиспользовать SQLite-соединение текущего контекста.

	Returns:
	    Соединение с sqlite3.Row и включёнными внешними ключами; коммит у вызывающего.
	Raises:
	    RuntimeError: Нет контекста Flask.
	    sqlite3.Error: Базу не удалось открыть или настроить."""
	connection = cast(sqlite3.Connection | None, g.get('db'))
	if connection is None:
		connection = sqlite3.connect(config.get().database, timeout=10)
		connection.row_factory = sqlite3.Row
		connection.execute('PRAGMA foreign_keys = ON')
		g.db = connection
	return connection

def initialize() -> None:
	"""Создать каталог БД и отсутствующие таблицы, сохранив имеющиеся данные.

	Raises:
	    OSError: Каталог БД или файл схемы недоступен.
	    sqlite3.Error: Не удалось открыть БД или выполнить схему."""
	config.get().database.parent.mkdir(parents=True, exist_ok=True)
	get().executescript(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))

def close(_error: BaseException | None = None) -> None:
	"""Закрыть соединение при завершении контекста Flask.

	Args:
	    _error: Исключение запроса, переданное Flask; на закрытие не влияет.
	Raises:
	    RuntimeError: Нет контекста Flask.
	    sqlite3.Error: Не удалось закрыть соединение."""
	connection = cast(sqlite3.Connection | None, g.pop('db', None))
	if connection is not None:
		connection.close()
