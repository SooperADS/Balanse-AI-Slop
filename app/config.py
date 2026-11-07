"""Чтение TOML-конфигурации и доступ к проверенным настройкам Flask."""

import hashlib
import tomllib

from pathlib import Path
from typing import TypedDict, cast
from werkzeug.security import generate_password_hash
from flask import current_app

def token_hash(value: str) -> str:
	"""Вернуть SHA-256 токена или пароля для поиска сессий и их отзыва.
	
	Хеш проверки пароля создаётся отдельно через Werkzeug; это не его замена."""
	return hashlib.sha256(value.encode()).hexdigest()

class AppConfig(TypedDict):
	"""Проверенные настройки приложения. Пароль TOML сюда не переносится открытым текстом.
	
	DATABASE — абсолютный путь SQLite; PASSWORD_HASH — хеш Werkzeug;
	PASSWORD_FINGERPRINT — стабильный признак пароля для отзыва сессий;
	MAX_LOGIN_ATTEMPTS — число попыток входа с одного IP за 15 минут."""
	DATABASE: str
	PASSWORD_HASH: str
	PASSWORD_FINGERPRINT: str
	MAX_LOGIN_ATTEMPTS: int

def load(root: Path) -> tuple[AppConfig, str, int]:
	"""Прочитать config.toml относительно root и вернуть настройки, адрес и порт.
	
	При отсутствии файла создать его из примера и поднять ValueError с подсказкой.
	Пустой пароль разрешён. Относительный путь базы отсчитывается от root."""
	path = root / 'config.toml'
	if not path.exists():
		with path.open('x', encoding='utf-8') as file:
			file.write((root / 'config.example.toml').read_text(encoding='utf-8'))

		path.chmod(0o600)
		raise ValueError(
			'Создан config.toml. Задайте в нём auth.password и снова запустите python main.py'
		)

	with path.open('rb') as file:
		config = tomllib.load(file)

	auth = config.get('auth', {})
	password, max_attempts = auth.get('password', ''), auth.get('max_attempts', 5)
	if not isinstance(password, str):
		raise ValueError('Задайте пароль в config.toml: [auth] → password')
	elif len(password) > 512:
		raise ValueError('Пароль слишком длинный: не более 512 символов')

	if type(max_attempts) is not int:
		raise ValueError('Проверьте auth.max_attempts в config.toml')
	elif max_attempts <= 0:
		raise ValueError('Максимальное количество попыток входа должно быть больше 0')
	
	server = config.get('server', {})
	host, port = server.get('host', '0.0.0.0'), server.get('port', 8080)
	if not isinstance(host, str) or not host or type(port) is not int or not 1 <= port <= 65535:
		raise ValueError('Проверьте server.host и server.port в config.toml')
	
	database = config.get('storage', {}).get('database', 'data/budget.sqlite3')
	if not isinstance(database, str) or not database:
		raise ValueError('Укажите путь storage.database в config.toml')

	return {
		'DATABASE': str((root / database).resolve()),
		'PASSWORD_HASH': generate_password_hash(password),
		'PASSWORD_FINGERPRINT': token_hash(password),
		'MAX_LOGIN_ATTEMPTS': max_attempts
	}, host, port

def get() -> AppConfig:
	"""Вернуть настройки активного приложения; вызывать внутри контекста Flask."""
	return cast(AppConfig, current_app.config)
