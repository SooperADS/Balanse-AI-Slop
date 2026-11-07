"""Чтение TOML и типизированные настройки единственного приложения процесса."""

import hashlib
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from flask import current_app
from werkzeug.security import generate_password_hash

# Настройки хранятся одним объектом, отдельно от встроенных параметров Flask.
CONFIG_KEY: Final = 'app.config'

def token_hash(value: str) -> str:
	"""Вернуть стабильный SHA-256 токена или пароля для поиска и отзыва сессий."""
	return hashlib.sha256(value.encode()).hexdigest()

@dataclass(frozen=True, slots=True)
class AppConfig:
	"""Проверенные настройки; исходный пароль в объекте не сохраняется.

	Хеш Werkzeug проверяет пароль, а стабильный fingerprint отзывает сессии.
	Эти значения выполняют разные задачи и не восстанавливаются друг из друга.
	"""

	database: Path
	password_hash: str
	password_fingerprint: str
	max_login_attempts: int = 5

	@classmethod
	def from_password(
		cls, database: Path, password: str, max_login_attempts: int = 5
	) -> 'AppConfig':
		"""Создать настройки с хешем проверки и стабильным отпечатком пароля."""
		return cls(
			database,
			generate_password_hash(password),
			token_hash(password),
			max_login_attempts
		)

def _section(data: Mapping[str, object], name: str) -> Mapping[str, object]:
	"""Получить таблицу TOML или сообщить о неверном типе секции."""
	section = data.get(name, {})
	if not isinstance(section, dict):
		raise ValueError(f'Секция [{name}] должна быть таблицей TOML')
	return cast(Mapping[str, object], section)

def load(root: Path) -> tuple[AppConfig, str, int]:
	"""Прочитать настройки и вернуть объект приложения, адрес и порт.

	При первом запуске создать config.toml с правами 600 и вывести подсказку.
	Пустой пароль разрешён; относительный путь базы отсчитывается от root.
	"""
	path = root / 'config.toml'
	if not path.exists():
		with path.open('x', encoding='utf-8') as file:
			file.write((root / 'config.example.toml').read_text(encoding='utf-8'))

		path.chmod(0o600)
		raise ValueError(
			'Создан config.toml. Задайте в нём auth.password и снова запустите python main.py'
		)

	with path.open('rb') as file:
		data = tomllib.load(file)

	auth = _section(data, 'auth')
	password = auth.get('password', '')
	max_attempts = auth.get('max_attempts', 5)
	if not isinstance(password, str):
		raise ValueError('Задайте пароль в config.toml: [auth] → password')
	elif len(password) > 512:
		raise ValueError('Пароль слишком длинный: не более 512 символов')
	elif type(max_attempts) is not int or max_attempts <= 0:
		raise ValueError('auth.max_attempts должен быть целым числом больше 0')

	server = _section(data, 'server')
	host, port = server.get('host', '0.0.0.0'), server.get('port', 8080)
	if not isinstance(host, str) or not host or type(port) is not int or not 1 <= port <= 65535:
		raise ValueError('Проверьте server.host и server.port в config.toml')

	storage = _section(data, 'storage')
	database = storage.get('database', 'data/budget.sqlite3')
	if not isinstance(database, str) or not database:
		raise ValueError('Укажите путь storage.database в config.toml')

	return AppConfig.from_password((root / database).resolve(), password, max_attempts), host, port

def get() -> AppConfig:
	"""Вернуть единый объект настроек из активного контекста Flask."""
	return cast(AppConfig, current_app.extensions[CONFIG_KEY])
