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
		cfg = config.load(ROOT)
		app = init(cfg)
	except (OSError, ValueError) as error:
		print(f'Не удалось запустить приложение: {error}', file=sys.stderr)
		return 1

	host, port = cfg.host, cfg.port
	print(f'Домашняя бухгалтерия: http://{host}:{port}', flush=True)
	public_link_base = cfg.public_link_base
	if cfg.is_public_host and public_link_base:
		print(f'С другого устройства: {public_link_base}', flush=True)
	
	serve(app, host=host, port=port, clear_untrusted_proxy_headers=True)
	return 0

if __name__ == '__main__':
	raise SystemExit(main())
