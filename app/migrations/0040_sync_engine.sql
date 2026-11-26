-- Sync engine: upstream tracking, verification, per-repository log, history.

-- Numeric GitHub id. It stays the same when a repository is renamed or moved.
ALTER TABLE repos ADD COLUMN github_id INTEGER;
-- 'ok' = present in the last listing, 'gone' = deleted or no longer visible.
ALTER TABLE repos ADD COLUMN upstream_state TEXT NOT NULL DEFAULT 'ok';
ALTER TABLE repos ADD COLUMN gone_since REAL;
-- Time of the last sync that ended with status ok or warn.
ALTER TABLE repos ADD COLUMN last_ok_at REAL;
ALTER TABLE repos ADD COLUMN last_verify_at REAL;
ALTER TABLE repos ADD COLUMN last_verify_status TEXT;
ALTER TABLE repos ADD COLUMN last_verify_message TEXT;

UPDATE repos SET last_ok_at = last_sync_at WHERE last_status = 'ok';

CREATE INDEX idx_repos_github_id ON repos(github_id);

-- One row per repository per job: clone, update or verify.
CREATE TABLE repo_sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id     INTEGER NOT NULL,
    job_id      INTEGER,
    started_at  REAL NOT NULL,
    finished_at REAL NOT NULL,
    action      TEXT NOT NULL,
    status      TEXT NOT NULL,
    message     TEXT,
    size_bytes  INTEGER,
    duration    REAL
);
CREATE INDEX idx_repo_sync_log_repo ON repo_sync_log(repo_id, id DESC);
CREATE INDEX idx_repo_sync_log_job ON repo_sync_log(job_id);

-- One row per completed full pull job. The dashboard chart reads it.
CREATE TABLE sync_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER,
    recorded_at REAL NOT NULL,
    repo_count  INTEGER NOT NULL,
    ok_count    INTEGER NOT NULL,
    fail_count  INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL
);
CREATE INDEX idx_sync_history_time ON sync_history(recorded_at);
