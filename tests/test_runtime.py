"""Проверки конфигурации и запуска настоящего HTTP-сервера в дочернем процессе."""

import http.client
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest

from pathlib import Path
from unittest.mock import patch
from werkzeug.security import check_password_hash

from app import config

import main

def load_config() -> tuple[config.AppConfig, str, int]:
	"""Загрузить настройки относительно подменяемого в тесте main.ROOT."""
	return config.load(main.ROOT)

class RuntimeTest(unittest.TestCase):
	"""Проверки файлов конфигурации и настоящего HTTP-ответа Waitress."""
	def setUp(self) -> None:
		"""Подготовить временную папку с примером конфигурации."""
		self.temp = tempfile.TemporaryDirectory()
		self.root = Path(self.temp.name)
		(self.root / 'config.example.toml').write_text(
			(main.ROOT / 'config.example.toml').read_text()
		)

	def tearDown(self) -> None:
		"""Удалить временную папку теста."""
		self.temp.cleanup()

	def config(self, text: str) -> tuple[config.AppConfig, str, int]:
		"""Записать TOML и прочитать его с временным корнем приложения."""
		(self.root / 'config.toml').write_text(text)
		with patch.object(main, 'ROOT', self.root):
			return load_config()

	def test_first_start_and_config_validation(self) -> None:
		"""Проверить создание конфигурации, пустой пароль, хеши и недопустимые значения."""
		with patch.object(main, 'ROOT', self.root):
			with self.assertRaisesRegex(ValueError, 'Создан config.toml'):
				load_config()

			self.assertEqual((self.root / 'config.toml').stat().st_mode & 0o777, 0o600)
			cfg, _, _ = load_config()
			self.assertTrue(check_password_hash(cfg.password_hash, ''))

		text = '[auth]\npassword="my secret"\n[server]\nhost="127.0.0.1"\nport=8081\n'
		config, host, port = self.config(text)

		self.assertEqual((host, port), ('127.0.0.1', 8081))
		self.assertEqual(config.database, self.root / 'data/budget.sqlite3')
		self.assertTrue(check_password_hash(
			config.password_hash, 'my secret'
		))

		same, _, _ = self.config(text)
		self.assertEqual(
			same.password_fingerprint, config.password_fingerprint
		)
		self.assertNotEqual(same.password_hash, config.password_hash)
		for bad in (
			'[auth]\npassword=42',
			'auth = false', 'server = []', 'storage = 42',
			'[auth]\npassword=""\nmax_attempts=0',
			'[auth]\npassword=""\nmax_attempts=true',
			'[auth]\npassword="' + 'x' * 513 + '"',
			text.replace('8081', '0'),
			text.replace('8081', 'true')):
			with self.subTest(bad=bad), self.assertRaises(ValueError):
				self.config(bad)

		with self.assertRaises(tomllib.TOMLDecodeError):
			self.config('not toml')

	def test_public_node_validation_and_password_repr(self) -> None:
		"""Проверить домены/IP с портом, запрет частей URL и скрытие пароля в repr."""
		for node, expected in (
			('', ''), ('budget.home', 'budget.home'),
			('192.168.1.10:8080', '192.168.1.10:8080'),
			('Бюджет.рф:80', 'xn--90agdd5d8c.xn--p1ai:80'),
			('[::1]:8080', '[::1]:8080'), ('[2001:db8::1]', '[2001:db8::1]'),
		):
			with self.subTest(node=node):
				cfg, _, _ = self.config('[auth]\npassword="private secret"\n[server]\npublic_node="' + node + '"')
				self.assertEqual(cfg.public_node, expected)
				self.assertEqual(cfg.password, 'private secret')
				self.assertNotIn('private secret', repr(cfg))
		for value in (
			'42', 'false', '"http://budget.home"', '"budget.home/path"',
			'"user:secret@budget.home"', '"budget.home?x=1"', '"budget.home#part"',
			'"budget.home:0"', '"budget.home:65536"', '"budget.home:"',
			'"budget.home:abc"', '"budget home"', '"::1"', '"-budget.home"',
		):
			with self.subTest(value=value), self.assertRaises(ValueError):
				self.config('[server]\npublic_node=' + value)

	def test_waitress_http_smoke(self) -> None:
		"""Запустить сервер в дочернем процессе, проверить вход и гарантированно остановить его."""
		with socket.socket() as probe:
			try:
				probe.bind(('127.0.0.1', 0))
			except PermissionError:
				self.skipTest('Среда запрещает локальные сетевые сокеты')
			port = probe.getsockname()[1]

		process = subprocess.Popen(
			[
				sys.executable, '-m', 'tests.runtime_server',
				str(self.root / 'smoke.sqlite3'), str(port),
			],
			stdout=subprocess.PIPE, stderr=subprocess.PIPE,
			cwd=Path(__file__).resolve().parents[1],
		)

		try:
			deadline = time.monotonic() + 10
			while time.monotonic() < deadline:
				connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
				try:
					connection.request('GET', '/login')
					response = connection.getresponse()
					self.assertEqual(response.status, 200)
					self.assertIn('Всё под контролем', response.read().decode())
					self.assertIn('budget_login_csrf=', response.getheader('Set-Cookie') or '')
					break
				except (ConnectionRefusedError, TimeoutError):
					if process.poll() is not None:
						assert process.stderr is not None
						self.fail(process.stderr.read().decode())
					time.sleep(.05)
				finally:
					connection.close()
			else:
				self.fail('Waitress не запустился за 10 секунд')
		finally:
			process.terminate()
			try:
				process.communicate(timeout=5)
			except subprocess.TimeoutExpired:
				process.kill()
				process.communicate()

if __name__ == '__main__':
	unittest.main()
