-- Baseline schema. IF NOT EXISTS because databases made before migrations
-- existed already have these tables.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name      TEXT UNIQUE NOT NULL,
    private        INTEGER NOT NULL DEFAULT 0,
    fork           INTEGER NOT NULL DEFAULT 0,
    archived       INTEGER NOT NULL DEFAULT 0,
    default_branch TEXT,
    mirror_path    TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    size_bytes     INTEGER,
    last_sync_at   REAL,
    last_status    TEXT,
    last_error     TEXT,
    discovered_at  REAL
);

CREATE TABLE IF NOT EXISTS job_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type    TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    ok_count    INTEGER DEFAULT 0,
    fail_count  INTEGER DEFAULT 0,
    summary     TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs_started ON job_runs(started_at DESC);
