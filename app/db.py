"""SQLite persistence.

Short-lived connections per operation keep things simple across the FastAPI
event loop and the APScheduler worker threads. WAL + a busy timeout handle the
occasional concurrent write.
"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .config import get_settings

SCHEMA = """
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
"""

# Keys seeded into the settings table on first boot, taken from env defaults.
SEED_KEYS = (
    "include_forks", "include_archived", "include_wikis", "fetch_lfs",
    "telegram_enabled", "telegram_bot_token", "telegram_chat_id",
    "notify_on_success", "notify_on_fail", "protection_delete",
    "schedule_scan", "schedule_pull", "schedule_protection",
)


@contextmanager
def get_conn():
    s = get_settings()
    Path(s.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(s.db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
    _seed_settings()


def _seed_settings() -> None:
    s = get_settings()
    with get_conn() as conn:
        existing = {r["key"] for r in conn.execute("SELECT key FROM settings")}
        for key in SEED_KEYS:
            if key in existing:
                continue
            val = getattr(s, key)
            if isinstance(val, bool):
                val = "1" if val else "0"
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)", (key, str(val))
            )


# --- settings ---------------------------------------------------------------

def get_setting(key: str, default: str | None = None) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def get_bool(key: str, default: bool = False) -> bool:
    val = get_setting(key)
    return default if val is None else val == "1"


def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def get_all_settings() -> dict[str, str]:
    with get_conn() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


# --- repos ------------------------------------------------------------------

def upsert_repo(repo: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO repos (full_name, private, fork, archived, default_branch,
                               mirror_path, discovered_at, enabled)
            VALUES (:full_name, :private, :fork, :archived, :default_branch,
                    :mirror_path, :discovered_at, 1)
            ON CONFLICT(full_name) DO UPDATE SET
                private        = excluded.private,
                fork           = excluded.fork,
                archived       = excluded.archived,
                default_branch = excluded.default_branch,
                mirror_path    = excluded.mirror_path
            """,
            repo,
        )


def list_repos(enabled_only: bool = False) -> list[sqlite3.Row]:
    q = "SELECT * FROM repos"
    if enabled_only:
        q += " WHERE enabled = 1"
    q += " ORDER BY full_name"
    with get_conn() as conn:
        return conn.execute(q).fetchall()


def get_repo(repo_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()


def toggle_repo(repo_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE repos SET enabled = 1 - enabled WHERE id = ?", (repo_id,)
        )


def record_repo_result(full_name: str, status: str, error: str | None,
                       size_bytes: int | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE repos SET last_sync_at = ?, last_status = ?, last_error = ?, "
            "size_bytes = COALESCE(?, size_bytes) WHERE full_name = ?",
            (time.time(), status, error, size_bytes, full_name),
        )


# --- job runs ---------------------------------------------------------------

def start_job(job_type: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO job_runs (job_type, status, started_at) VALUES (?, 'running', ?)",
            (job_type, time.time()),
        )
        return cur.lastrowid


def finish_job(job_id: int, status: str, ok: int, fail: int,
               summary: str, detail: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE job_runs SET status = ?, finished_at = ?, ok_count = ?, "
            "fail_count = ?, summary = ?, detail = ? WHERE id = ?",
            (status, time.time(), ok, fail, summary, detail, job_id),
        )


def recent_jobs(limit: int = 25) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM job_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()


def get_job(job_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM job_runs WHERE id = ?", (job_id,)).fetchone()
