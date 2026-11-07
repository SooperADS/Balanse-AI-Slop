"""Вспомогательный HTTP-сервер: python -m tests.runtime_server DATABASE PORT."""

import sys
from pathlib import Path

from waitress.server import create_server

from app import config, init

def main(database_path: str, port: int) -> None:
	"""Создать временное приложение и обслуживать HTTP до завершения процесса родителем."""
	app = init(config.AppConfig.from_password(Path(database_path), 'smoke-test'))
	server = create_server(app, host='127.0.0.1', port=port)
	print('READY', flush=True)
	server.run()

if __name__ == '__main__':
	main(sys.argv[1], int(sys.argv[2]))
