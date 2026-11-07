import hashlib
import tomllib

from pathlib import Path
from typing import TypedDict, cast
from werkzeug.security import generate_password_hash
from flask import current_app

def token_hash(value: str) -> str:
	return hashlib.sha256(value.encode()).hexdigest()

class AppConfig(TypedDict):
	DATABASE: str
	PASSWORD_HASH: str
	PASSWORD_FINGERPRINT: str
	MAX_LOGIN_ATTEMPTS: int

def load(root: Path) -> tuple[AppConfig, str, int]:
	path = root / 'config.toml'
	if not path.exists():
		with path.open('x', encoding='utf-8') as file:
			file.write((root / 'config.example.toml').read_text(encoding='utf-8'))

		path.chmod(0o600)
		raise ValueError(
			'Создан config.toml. Задайте в нём auth.password и снова запустите python app.py'
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
	return cast(AppConfig, current_app.config)
