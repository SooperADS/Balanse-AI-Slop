"""Запуск HTTP-сервера Waitress: python main.py после установки requirements.txt."""

import sys

from pathlib import Path
from waitress import serve

from app import config, init

# Корень для config.toml, примера настроек и относительного пути базы.
ROOT: Path = Path(__file__).resolve().parent

def main() -> int:
	"""Загрузить настройки и запустить сервер; вернуть 1 при ошибке конфигурации."""
	try:
		cfg_obj, host, port = config.load(ROOT)
		app = init(cfg_obj)
	except (OSError, ValueError) as error:
		print(f'Не удалось запустить приложение: {error}', file=sys.stderr)
		return 1

	print(f'Домашняя бухгалтерия: http://{host}:{port}', flush=True)
	if host != '127.0.0.1':
		print(f'С другого устройства: http://<локальный-IP-компьютера>:{port}', flush=True)
	
	serve(app, host=host, port=port, clear_untrusted_proxy_headers=True)
	return 0

if __name__ == '__main__':
	raise SystemExit(main())
