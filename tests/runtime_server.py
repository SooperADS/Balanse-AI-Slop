"""Сервер для проверки HTTP-запуска в отдельном процессе."""

import sys

from waitress.server import create_server
from werkzeug.security import generate_password_hash

from app import init

def main(database_path: str, port: int) -> None:
	app = init({
		'DATABASE': database_path,
		'PASSWORD_HASH': generate_password_hash('smoke-test'),
		'MAX_LOGIN_ATTEMPTS': 5,
	})
	server = create_server(app, host='127.0.0.1', port=port)
	print('READY', flush=True)
	server.run()

if __name__ == '__main__':
	main(sys.argv[1], int(sys.argv[2]))
