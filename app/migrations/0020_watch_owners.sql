-- Users and organisations whose public repositories the scan job adds as
-- source='watched'. NOCASE because GitHub logins ignore case.
CREATE TABLE watch_owners (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    login            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind             TEXT NOT NULL CHECK (kind IN ('user', 'org')),
    include_forks    INTEGER NOT NULL DEFAULT 0,
    include_archived INTEGER NOT NULL DEFAULT 0,
    added_at         REAL NOT NULL,
    last_scan_at     REAL,
    last_error       TEXT
);
