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
	return config.load(main.ROOT)

class RuntimeTest(unittest.TestCase):
	def setUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.root = Path(self.temp.name)
		(self.root / 'config.example.toml').write_text(
			(main.ROOT / 'config.example.toml').read_text()
		)

	def tearDown(self) -> None:
		self.temp.cleanup()

	def config(self, text: str) -> tuple[config.AppConfig, str, int]:
		(self.root / 'config.toml').write_text(text)
		with patch.object(main, 'ROOT', self.root):
			return load_config()

	def test_first_start_and_config_validation(self) -> None:
		with patch.object(main, 'ROOT', self.root):
			with self.assertRaisesRegex(ValueError, 'Создан config.toml'):
				load_config()

			self.assertEqual((self.root / 'config.toml').stat().st_mode & 0o777, 0o600)
			cfg, _, _ = load_config()
			self.assertTrue(check_password_hash(cfg['PASSWORD_HASH'], ''))

		text = '[auth]\npassword="my secret"\n[server]\nhost="127.0.0.1"\nport=8081\n'
		config, host, port = self.config(text)

		self.assertEqual((host, port), ('127.0.0.1', 8081))
		self.assertEqual(config['DATABASE'], str(self.root / 'data/budget.sqlite3'))
		self.assertTrue(check_password_hash(
			config['PASSWORD_HASH'], 'my secret'
		))

		same, _, _ = self.config(text)
		self.assertEqual(
			same['PASSWORD_FINGERPRINT'], config['PASSWORD_FINGERPRINT']
		)
		self.assertNotEqual(same['PASSWORD_HASH'], config['PASSWORD_HASH'])
		for bad in (
			'[auth]\npassword=42',
			'[auth]\npassword="' + 'x' * 513 + '"',
			text.replace('8081', '0'),
			text.replace('8081', 'true')):
			with self.subTest(bad=bad), self.assertRaises(ValueError):
				self.config(bad)

		with self.assertRaises(tomllib.TOMLDecodeError):
			self.config('not toml')

	def test_waitress_http_smoke(self) -> None:
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
