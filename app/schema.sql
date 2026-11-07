PRAGMA foreign_keys = ON;
-- Независимые бюджеты; удаление профиля каскадно удаляет принадлежащие ему данные.
CREATE TABLE IF NOT EXISTS profiles (
	id INTEGER PRIMARY KEY,
	name TEXT NOT NULL CHECK(
		length(trim(name)) BETWEEN 1 AND 80
	)
);
-- Начальный капитал в целых сотых, уникальный для пары профиль/валюта.
CREATE TABLE IF NOT EXISTS capitals (
	profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
	currency TEXT NOT NULL CHECK(currency IN ('RUB', 'USD', 'EUR')),
	amount INTEGER NOT NULL,
	PRIMARY KEY(profile_id, currency)
);
-- Пользовательские категории. Отсутствие категории хранится как NULL в entries.
CREATE TABLE IF NOT EXISTS categories (
	id INTEGER PRIMARY KEY,
	profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
	name TEXT NOT NULL CHECK(
		length(trim(name)) BETWEEN 1 AND 80 AND name != 'Без категории'
	),
	UNIQUE(profile_id, name),
	UNIQUE(id, profile_id)
);
-- Метки профиля; имя уникально внутри профиля.
CREATE TABLE IF NOT EXISTS tags (
	id INTEGER PRIMARY KEY,
	profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
	name TEXT NOT NULL CHECK(
		length(trim(name)) BETWEEN 1 AND 80
	),
	UNIQUE(profile_id, name),
	UNIQUE(id, profile_id)
);
-- Движение денег: знаковая исходная сумма и необязательная сторона получения.
-- Тип, курс и плановый статус вычисляются; категория может отсутствовать.
CREATE TABLE IF NOT EXISTS entries (
	id INTEGER PRIMARY KEY,
	profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
	currency TEXT NOT NULL CHECK(currency IN ('RUB', 'USD', 'EUR')),
	amount INTEGER NOT NULL CHECK(amount != 0),
	destination_currency TEXT CHECK(destination_currency IN ('RUB', 'USD', 'EUR')),
	destination_amount INTEGER CHECK(destination_amount > 0),
	occurred_at TEXT NOT NULL,
	comment TEXT NOT NULL CHECK(
		length(trim(comment)) BETWEEN 1 AND 2000
	),
	category_id INTEGER,
	UNIQUE(id, profile_id),
	FOREIGN KEY(category_id, profile_id) REFERENCES categories(id, profile_id),
	CHECK(
		(destination_currency IS NULL AND destination_amount IS NULL)
		OR (destination_currency IS NOT NULL AND destination_amount IS NOT NULL
			AND amount < 0 AND destination_currency != currency)
	)
);
-- Журнал выбирается внутри профиля от новых дат к старым.
CREATE INDEX IF NOT EXISTS entries_by_profile_date ON entries(profile_id, occurred_at DESC);
-- Связи многие-ко-многим; составные FK проверяют общий профиль записи и тега.
CREATE TABLE IF NOT EXISTS entry_tags (
	entry_id INTEGER NOT NULL,
	tag_id INTEGER NOT NULL,
	profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
	PRIMARY KEY(entry_id, tag_id),
	FOREIGN KEY(entry_id, profile_id) REFERENCES entries(id, profile_id) ON DELETE CASCADE,
	FOREIGN KEY(tag_id, profile_id) REFERENCES tags(id, profile_id) ON DELETE CASCADE
);
-- Постоянные сессии: хеш токена, отпечаток пароля, CSRF и текущий профиль.
CREATE TABLE IF NOT EXISTS auth_sessions (
	token_hash TEXT PRIMARY KEY,
	password_fingerprint TEXT NOT NULL,
	csrf_token TEXT NOT NULL,
	profile_id INTEGER REFERENCES profiles(id) ON DELETE
	SET NULL
);
-- Окно ограничения попыток входа по IP; окно очищается обработчиком входа.
CREATE TABLE IF NOT EXISTS login_attempts (
	address TEXT PRIMARY KEY,
	count INTEGER NOT NULL,
	window_started REAL NOT NULL
);