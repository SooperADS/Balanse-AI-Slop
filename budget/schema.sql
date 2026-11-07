PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS profiles (
	id INTEGER PRIMARY KEY,
	name TEXT NOT NULL CHECK(
		length(trim(name)) BETWEEN 1 AND 80
	)
);
CREATE TABLE IF NOT EXISTS capitals (
	profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
	currency TEXT NOT NULL CHECK(currency IN ('RUB', 'USD', 'EUR')),
	amount INTEGER NOT NULL,
	PRIMARY KEY(profile_id, currency)
);
CREATE TABLE IF NOT EXISTS categories (
	id INTEGER PRIMARY KEY,
	profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
	name TEXT NOT NULL CHECK(
		length(trim(name)) BETWEEN 1 AND 80
	),
	is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0, 1)),
	UNIQUE(profile_id, name),
	UNIQUE(id, profile_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_default_category ON categories(profile_id)
WHERE is_default = 1;
CREATE TABLE IF NOT EXISTS tags (
	id INTEGER PRIMARY KEY,
	profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
	name TEXT NOT NULL CHECK(
		length(trim(name)) BETWEEN 1 AND 80
	),
	UNIQUE(profile_id, name),
	UNIQUE(id, profile_id)
);
CREATE TABLE IF NOT EXISTS entries (
	id INTEGER PRIMARY KEY,
	profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
	kind TEXT NOT NULL CHECK(kind IN ('income', 'expense')),
	currency TEXT NOT NULL CHECK(currency IN ('RUB', 'USD', 'EUR')),
	amount INTEGER NOT NULL CHECK(amount > 0),
	occurred_at TEXT NOT NULL,
	comment TEXT NOT NULL CHECK(
		length(trim(comment)) BETWEEN 1 AND 2000
	),
	category_id INTEGER NOT NULL,
	UNIQUE(id, profile_id),
	FOREIGN KEY(category_id, profile_id) REFERENCES categories(id, profile_id)
);
CREATE INDEX IF NOT EXISTS entries_by_profile_date ON entries(profile_id, occurred_at DESC);
CREATE TABLE IF NOT EXISTS entry_tags (
	entry_id INTEGER NOT NULL,
	tag_id INTEGER NOT NULL,
	profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
	PRIMARY KEY(entry_id, tag_id),
	FOREIGN KEY(entry_id, profile_id) REFERENCES entries(id, profile_id) ON DELETE CASCADE,
	FOREIGN KEY(tag_id, profile_id) REFERENCES tags(id, profile_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS auth_sessions (
	token_hash TEXT PRIMARY KEY,
	password_fingerprint TEXT NOT NULL,
	csrf_token TEXT NOT NULL,
	profile_id INTEGER REFERENCES profiles(id) ON DELETE
	SET NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
	address TEXT PRIMARY KEY,
	count INTEGER NOT NULL,
	window_started REAL NOT NULL
);