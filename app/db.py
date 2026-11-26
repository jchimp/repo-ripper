"""SQLite persistence.

Short-lived connections per operation keep things simple across the FastAPI
event loop and the APScheduler worker threads. WAL + a busy timeout handle the
occasional concurrent write.
"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import migrations
from .config import get_settings

# Keys seeded into the settings table on first boot, taken from env defaults.
SEED_KEYS = (
    "include_forks", "include_archived", "include_wikis", "fetch_lfs",
    "telegram_enabled", "telegram_bot_token", "telegram_chat_id",
    "notify_on_success", "notify_on_fail", "protection_delete",
    "schedule_scan", "schedule_pull", "schedule_protection",
    "auto_enable_new",
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
        migrations.apply_all(conn)
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

def upsert_repo(repo: dict, enabled: bool = True, update_enabled: bool = False) -> bool:
    """Insert a repository, or refresh the GitHub facts of an existing row.

    `repo["source"]` is 'mine' (default: found in the token's own list) or
    'watched' (added by hand or by a watched owner). A new 'watched' row gets
    `metadata_enabled=0`, because a public project can have a very large
    number of issues.

    An existing row keeps its `enabled`, `metadata_enabled` and `source`, with
    two exceptions: a 'watched' row that shows up in the token's own list
    becomes 'mine' (a 'mine' row never becomes 'watched'), and
    `update_enabled` sets the backup switch.

    Args:
        repo: The row values (full_name, private, fork, archived,
            default_branch, mirror_path, discovered_at, optional source and
            metadata_enabled).
        enabled: The backup switch for a NEW row.
        update_enabled: Also set the switch of an existing row. Off by
            default, so that a scan never changes the owner's choice.

    Returns:
        True when a new row was inserted.
    """
    row = {"source": "mine", **repo, "enabled": 1 if enabled else 0}
    row.setdefault("metadata_enabled", 1 if row["source"] == "mine" else 0)
    sql = """
        INSERT INTO repos (full_name, private, fork, archived, default_branch,
                           mirror_path, discovered_at, enabled, source,
                           metadata_enabled)
        VALUES (:full_name, :private, :fork, :archived, :default_branch,
                :mirror_path, :discovered_at, :enabled, :source, :metadata_enabled)
        ON CONFLICT(full_name) DO UPDATE SET
            private        = excluded.private,
            fork           = excluded.fork,
            archived       = excluded.archived,
            default_branch = excluded.default_branch,
            mirror_path    = excluded.mirror_path,
            source         = CASE WHEN excluded.source = 'mine'
                                  THEN 'mine' ELSE repos.source END
    """
    if update_enabled:
        sql += ", enabled = excluded.enabled"
    with get_conn() as conn:
        existed = conn.execute(
            "SELECT 1 FROM repos WHERE full_name = ?", (row["full_name"],)
        ).fetchone()
        conn.execute(sql, row)
    return existed is None


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


def count_repos() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]


def get_repo_by_name(full_name: str) -> sqlite3.Row | None:
    """Find a repository by `owner/name`. GitHub names ignore case."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM repos WHERE full_name = ? COLLATE NOCASE", (full_name,)
        ).fetchone()


def delete_watched_repo(repo_id: int) -> bool:
    """Remove one 'watched' row. Files on disk are not touched.

    Returns:
        True when a row was removed. A 'mine' row is never removed.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM repos WHERE id = ? AND source = 'watched'", (repo_id,)
        )
        return cur.rowcount == 1


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


# --- watched owners ---------------------------------------------------------

def add_watch_owner(login: str, kind: str, include_forks: bool,
                    include_archived: bool) -> bool:
    """Add a watched owner. Returns False when the login is already there."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO watch_owners (login, kind, include_forks, include_archived, "
            "added_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(login) DO NOTHING",
            (login, kind, int(include_forks), int(include_archived), time.time()),
        )
        return cur.rowcount == 1


def list_watch_owners() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM watch_owners ORDER BY login COLLATE NOCASE"
        ).fetchall()


def get_watch_owner(owner_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM watch_owners WHERE id = ?", (owner_id,)
        ).fetchone()


def get_watch_owner_by_login(login: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM watch_owners WHERE login = ?", (login,)
        ).fetchone()


def remove_watch_owner(owner_id: int) -> None:
    """Remove a watched owner. Its repositories stay in the repos table."""
    with get_conn() as conn:
        conn.execute("DELETE FROM watch_owners WHERE id = ?", (owner_id,))


def record_watch_owner_scan(owner_id: int, error: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE watch_owners SET last_scan_at = ?, last_error = ? WHERE id = ?",
            (time.time(), error, owner_id),
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
