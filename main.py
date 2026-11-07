"""Запуск: python app.py (после установки requirements.txt)."""

import sys
import tomllib

from pathlib import Path
from waitress import serve

from app import init
from app import config

ROOT: Path = Path(__file__).resolve().parent

def main() -> int:
	try:
		cfg_obj, host, port = config.load(ROOT)
		app = init(cfg_obj)
	except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
		print(f'Не удалось запустить приложение: {error}', file=sys.stderr)
		return 1

	print(f'Домашняя бухгалтерия: http://{host}:{port}', flush=True)
	if host != '127.0.0.1':
		print(f'С другого устройства: http://<локальный-IP-компьютера>:{port}', flush=True)
	
	serve(app, host=host, port=port, clear_untrusted_proxy_headers=True)
	return 0

if __name__ == '__main__':
	raise SystemExit(main())
