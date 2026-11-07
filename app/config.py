"""Чтение TOML и типизированные настройки единственного приложения процесса."""

import hashlib
import ipaddress
import re
import tomllib

from flask import current_app
from pathlib import Path
from typing import Final, cast
from urllib.parse import urlsplit
from collections.abc import Mapping
from dataclasses import dataclass, field
from werkzeug.security import generate_password_hash

# Настройки хранятся одним объектом, отдельно от встроенных параметров Flask.
CONFIG_KEY: Final = 'app.config'

def token_hash(value: str) -> str:
	"""Вычислить стабильный SHA-256 для поиска и отзыва сессий.

	Args:
	    value: Исходный токен или пароль.
	Returns:
	    Хеш в шестнадцатеричной записи."""
	return hashlib.sha256(value.encode()).hexdigest()

@dataclass(frozen=True, slots=True)
class AppConfig:
	"""Настройки БД, входа и публичных ссылок.

	database — путь SQLite; max_login_attempts — лимит попыток за 15 минут.
	password_hash проверяет пароль, password_fingerprint отзывает сессии.
	password нужен для приглашений и исключён из repr; public_node — хост[:порт].
	"""

	database: Path
	password_hash: str
	password_fingerprint: str
	max_login_attempts: int = 5
	password: str = field(default='', repr=False)
	public_node: str = ''

	@property
	def public_link_base(self) -> str | None:
		return f'http://{self.public_node}' if self.public_node != '' else None

	@classmethod
	def from_password(
		cls,
		database: Path,
		password: str,
		max_login_attempts: int = 5, *,
		public_node: str = '',
	) -> 'AppConfig':
		"""Подготовить настройки из открытого пароля.

		Args:
		    database: Путь SQLite.
		    password: Общий пароль; пустая строка разрешена.
		    max_login_attempts: Допустимые попытки с одного IP за 15 минут.
		    public_node: Публичный хост[:порт]; пустая строка отключает приглашения.
		Returns:
		    Настройки с хешем, отпечатком и паролем для ссылок.
		Raises:
		    ValueError: Публичный адрес содержит схему, путь или неверный хост/порт."""
		public_node = _public_node(public_node)
		return cls(
			database,
			generate_password_hash(password),
			token_hash(password),
			max_login_attempts,
			password,
			public_node,
		)

def _public_node(raw: object) -> str:
	"""Проверить и нормализовать публичный узел без схемы и пути.

	Args:
	    raw: Хост[:порт] из TOML; IPv6 записывается в квадратных скобках.
	Returns:
	    Узел с ASCII-доменом либо пустая строка для отключённых приглашений.
	Raises:
	    ValueError: Неверный тип, хост, порт или присутствуют лишние части URL.
	"""
	message = 'server.public_node: укажите хост или IP с портом, без схемы и пути'
	if not isinstance(raw, str):
		raise ValueError(message)
	elif not raw:
		return ''
	elif any(char.isspace() for char in raw) or any(char in raw for char in '/\\@?#%'):
		raise ValueError(message)

	try:
		parts = urlsplit('//' + raw)
		host, port = parts.hostname, parts.port
		if not host or raw.endswith(':') or (port is not None and not 1 <= port <= 65535):
			raise ValueError(message)
		elif ':' in host:
			host = '[' + ipaddress.IPv6Address(host).compressed + ']'
		else:
			host = host.encode('idna').decode('ascii').lower()
			if len(host) > 253 or any(
				not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
				for label in host.rstrip('.').split('.')
			):
				raise ValueError(message)

		return host + (f':{port}' if port is not None else '')
	except (ValueError, UnicodeError):
		raise ValueError(message) from None

def _section(data: Mapping[str, object], name: str) -> Mapping[str, object]:
	"""Получить таблицу TOML; отсутствующая секция считается пустой.

	Args:
	    data: Разобранная конфигурация.
	    name: Имя секции.
	Returns:
	    Содержимое секции.
	Raises:
	    ValueError: Секция имеет тип, отличный от таблицы TOML."""
	section = data.get(name, {})
	if not isinstance(section, dict):
		raise ValueError(f'Секция [{name}] должна быть таблицей TOML')
	return cast(Mapping[str, object], section)

def load(root: Path) -> tuple[AppConfig, str, int]:
	"""Прочитать конфигурацию; при первом запуске создать файл с правами 600.

	Args:
	    root: Каталог конфигурации и база относительных путей SQLite.
	Returns:
	    Настройки приложения, адрес прослушивания и порт.
	Raises:
	    ValueError: Файл только создан, TOML неверен или настройки недопустимы.
	    OSError: Файл конфигурации недоступен для чтения или записи."""
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
	public_node = _public_node(server.get('public_node', ''))

	storage = _section(data, 'storage')
	database = storage.get('database', 'data/budget.sqlite3')
	if not isinstance(database, str) or not database:
		raise ValueError('Укажите путь storage.database в config.toml')

	return AppConfig.from_password(
		(root / database).resolve(),
		password,
		max_attempts,
		public_node=public_node,
	), host, port

def get() -> AppConfig:
	"""Получить настройки единственного приложения в активном контексте.

	Returns:
	    Объект AppConfig, установленный init().
	Raises:
	    RuntimeError: Нет контекста Flask.
	    KeyError: Приложение ещё не настроено."""
	return cast(AppConfig, current_app.extensions[CONFIG_KEY])
