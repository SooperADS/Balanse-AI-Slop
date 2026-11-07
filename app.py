"""Запуск: python app.py (после установки requirements.txt)."""
from pathlib import Path
import sys
import tomllib

from werkzeug.security import generate_password_hash
from waitress import serve

from budget.auth import token_hash
from budget.web import create_app

ROOT = Path(__file__).resolve().parent


def load_config():
    path = ROOT / 'config.toml'
    if not path.exists():
        with path.open('x', encoding='utf-8') as file:
            file.write((ROOT / 'config.example.toml').read_text(encoding='utf-8'))
        path.chmod(0o600)
        raise ValueError('Создан config.toml. Задайте в нём auth.password и снова запустите python app.py.')
    with path.open('rb') as file:
        config = tomllib.load(file)
    password = config.get('auth', {}).get('password', '')
    if not isinstance(password, str) or not password.strip():
        raise ValueError('Задайте непустой пароль в config.toml: [auth] → password.')
    if len(password) > 4096:
        raise ValueError('Пароль слишком длинный: не более 4096 символов.')
    server = config.get('server', {})
    host, port = server.get('host', '0.0.0.0'), server.get('port', 8080)
    if not isinstance(host, str) or not host or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Проверьте server.host и server.port в config.toml.')
    database = config.get('storage', {}).get('database', 'data/budget.sqlite3')
    if not isinstance(database, str) or not database:
        raise ValueError('Укажите путь storage.database в config.toml.')
    return {'DATABASE': str((ROOT / database).resolve()), 'PASSWORD_HASH': generate_password_hash(password),
            'PASSWORD_FINGERPRINT': token_hash(password)}, host, port


def main():
    try:
        config, host, port = load_config()
        app = create_app(config)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f'Не удалось запустить приложение: {error}', file=sys.stderr)
        return 1
    print(f'Домашняя бухгалтерия: http://127.0.0.1:{port}', flush=True)
    if host != '127.0.0.1':
        print(f'С другого устройства: http://<локальный-IP-компьютера>:{port}', flush=True)
    serve(app, host=host, port=port, clear_untrusted_proxy_headers=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
