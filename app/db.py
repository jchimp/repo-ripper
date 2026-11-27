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
# Settings that have no env variable in config.py: key -> default value.
SEED_DEFAULTS = {
    "metadata_enabled": "1",
    "assets_keep_releases": "10",   # newest releases that get file downloads; 0 = all
    "assets_max_mb": "2048",        # size limit for one release file
}


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
        for key, val in SEED_DEFAULTS.items():
            if key not in existing:
                conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, val))


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


def set_settings(values: dict[str, str]) -> None:
    """Store many settings in one transaction: all of them, or none.

    Args:
        values: Setting key to new value.
    """
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(key, str(value)) for key, value in values.items()],
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
    """Store the result of one sync. `error` also holds the text of a warning."""
    now = time.time()
    with get_conn() as conn:
        conn.execute(
            "UPDATE repos SET last_sync_at = ?, last_status = ?, last_error = ?, "
            "size_bytes = COALESCE(?, size_bytes), "
            "last_ok_at = CASE WHEN ? IN ('ok', 'warn') THEN ? ELSE last_ok_at END "
            "WHERE full_name = ?",
            (now, status, error, size_bytes, status, now, full_name),
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


def toggle_repo_metadata(repo_id: int) -> None:
    """Switch the metadata capture of one repository on or off."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE repos SET metadata_enabled = 1 - metadata_enabled WHERE id = ?",
            (repo_id,),
        )


def list_repos_for_metadata() -> list[sqlite3.Row]:
    """Repositories that get a metadata capture, the oldest capture first.

    A repository with no capture yet comes before all others, so that a job
    that stopped on a rate limit continues with the work that is not done.
    """
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM repos WHERE enabled = 1 AND metadata_enabled = 1 "
            "ORDER BY meta_sync_at IS NOT NULL, meta_sync_at, full_name"
        ).fetchall()


def record_metadata_result(full_name: str, status: str, message: str,
                           issues: int | None, pulls: int | None) -> None:
    """Store the result of a metadata capture. A count of None keeps the old count."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE repos SET meta_sync_at = ?, meta_status = ?, meta_message = ?, "
            "meta_issues = COALESCE(?, meta_issues), meta_pulls = COALESCE(?, meta_pulls) "
            "WHERE full_name = ?",
            (time.time(), status, message, issues, pulls, full_name),
        )


def toggle_repo_assets(repo_id: int) -> None:
    """Switch the download of release files of one repository on or off."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE repos SET assets_enabled = 1 - assets_enabled WHERE id = ?", (repo_id,)
        )


def list_repos_for_assets() -> list[sqlite3.Row]:
    """Repositories that get release file downloads, the oldest run first.

    Release files need `releases.json`, so the metadata switch must be on too.
    """
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM repos WHERE enabled = 1 AND metadata_enabled = 1 "
            "AND assets_enabled = 1 "
            "ORDER BY assets_sync_at IS NOT NULL, assets_sync_at, full_name"
        ).fetchall()


def assets_coverage() -> tuple[int, int]:
    """Count the active repositories, and those without release file downloads.

    Active = backup on and not gone upstream: the repositories that the
    backup is expected to cover.

    Returns:
        `(active repositories with the release files switch off, active repositories)`.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(assets_enabled = 0), 0), COUNT(*) FROM repos "
            "WHERE enabled = 1 AND upstream_state != 'gone'"
        ).fetchone()
    return int(row[0]), int(row[1])


def set_repos_assets(repo_ids: list[int], enabled: bool) -> int:
    """Turn the release files switch on or off for several repositories.

    Turning it on changes only repositories that have the metadata switch on,
    because the list of releases comes from the metadata.

    Returns:
        The number of repositories that now have the requested state.
    """
    if not repo_ids:
        return 0
    marks = ", ".join("?" * len(repo_ids))
    sql = f"UPDATE repos SET assets_enabled = ? WHERE id IN ({marks})"
    if enabled:
        sql += " AND metadata_enabled = 1"
    with get_conn() as conn:
        conn.execute(sql, [1 if enabled else 0, *repo_ids])
        return conn.execute(
            f"SELECT COUNT(*) FROM repos WHERE assets_enabled = ? AND id IN ({marks})",
            [1 if enabled else 0, *repo_ids],
        ).fetchone()[0]


def record_assets_result(full_name: str, status: str, message: str,
                         files: int, total_bytes: int) -> None:
    """Store the result of a release files run for one repository."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE repos SET assets_sync_at = ?, assets_status = ?, assets_message = ?, "
            "assets_files = ?, assets_bytes = ? WHERE full_name = ?",
            (time.time(), status, message, files, total_bytes, full_name),
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


# --- added by the merged branch ---------------------------------------------


# Sort keys of the Repositories page -> ORDER BY. Fixed text only: the key
# comes from the query string and must never reach the SQL itself.
REPO_SORTS: dict[str, str] = {
    "name": "full_name COLLATE NOCASE",
    "name_desc": "full_name COLLATE NOCASE DESC",
    "synced": "last_sync_at IS NULL, last_sync_at DESC, full_name COLLATE NOCASE",
    "synced_old": "last_sync_at IS NOT NULL, last_sync_at, full_name COLLATE NOCASE",
    "size": "COALESCE(size_bytes, 0) DESC, full_name COLLATE NOCASE",
    "size_small": "COALESCE(size_bytes, 0), full_name COLLATE NOCASE",
    "status": ("CASE COALESCE(last_status, '') WHEN 'error' THEN 0 WHEN 'warn' THEN 1 "
               "WHEN '' THEN 2 ELSE 3 END, full_name COLLATE NOCASE"),
}


def query_repos(*, q: str = "", status: str = "", visibility: str = "", source: str = "",
                backup: str = "", flag: str = "", sort: str = "name",
                running: set[str] | None = None) -> list[sqlite3.Row]:
    """Return the repositories that match the filters of the Repositories page.

    Args:
        q: Text that the full name must contain.
        status: 'ok', 'warn', 'error', 'pending' (never synced) or 'running'.
        visibility: 'public' or 'private'.
        source: 'mine' or 'watched'.
        backup: 'on' or 'off'.
        flag: 'gone' (gone upstream) or 'verify' (verification failed).
        sort: A key of `REPO_SORTS`.
        running: Full names in work now; used by the 'running' status filter.

    An unknown value in a filter is ignored.
    """
    where: list[str] = []
    args: list[object] = []
    if q:
        # "!" is the escape character, so that % and _ in the text are literal.
        escaped = q.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        where.append("full_name LIKE ? ESCAPE '!'")
        args.append(f"%{escaped}%")
    if status in ("ok", "warn", "error"):
        where.append("last_status = ?")
        args.append(status)
    elif status == "pending":
        where.append("last_status IS NULL")
    elif status == "running":
        names = sorted(running or ())
        if not names:
            return []
        where.append(f"full_name IN ({', '.join('?' * len(names))})")
        args.extend(names)
    if visibility in ("public", "private"):
        where.append("private = ?")
        args.append(1 if visibility == "private" else 0)
    if source in ("mine", "watched"):
        where.append("source = ?")
        args.append(source)
    if backup in ("on", "off"):
        where.append("enabled = ?")
        args.append(1 if backup == "on" else 0)
    if flag == "gone":
        where.append("upstream_state = 'gone'")
    elif flag == "verify":
        where.append("last_verify_status = 'error'")
    sql = "SELECT * FROM repos"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + REPO_SORTS.get(sort, REPO_SORTS["name"])
    with get_conn() as conn:
        return conn.execute(sql, args).fetchall()


def get_repos(repo_ids: list[int]) -> list[sqlite3.Row]:
    """Return the rows with these ids, by name. Unknown ids are left out."""
    if not repo_ids:
        return []
    marks = ", ".join("?" * len(repo_ids))
    with get_conn() as conn:
        return conn.execute(
            f"SELECT * FROM repos WHERE id IN ({marks}) ORDER BY full_name", repo_ids
        ).fetchall()


def set_repos_enabled(repo_ids: list[int], enabled: bool) -> int:
    """Turn the backup on or off for several repositories. Returns the row count."""
    if not repo_ids:
        return 0
    marks = ", ".join("?" * len(repo_ids))
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE repos SET enabled = ? WHERE id IN ({marks})",
            [1 if enabled else 0, *repo_ids],
        )
        return cur.rowcount


def set_github_id(repo_id: int, github_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE repos SET github_id = ? WHERE id = ?", (github_id, repo_id))


def rename_repo(repo_id: int, full_name: str, mirror_path: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE repos SET full_name = ?, mirror_path = ? WHERE id = ?",
            (full_name, mirror_path, repo_id),
        )


def delete_unsynced_repo(repo_id: int) -> bool:
    """Delete a row that was never synced and has no GitHub id.

    The scan uses it for the empty row that it made for the new name of a
    renamed repository. Returns False when the row has data and was kept.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM repos WHERE id = ? AND github_id IS NULL AND last_sync_at IS NULL",
            (repo_id,),
        )
        return cur.rowcount == 1


def set_upstream_state(repo_id: int, state: str) -> None:
    """Set 'ok' or 'gone'. The time first seen gone is kept while it stays gone."""
    with get_conn() as conn:
        if state == "gone":
            conn.execute(
                "UPDATE repos SET upstream_state = 'gone', "
                "gone_since = COALESCE(gone_since, ?) WHERE id = ?",
                (time.time(), repo_id),
            )
        else:
            conn.execute(
                "UPDATE repos SET upstream_state = 'ok', gone_since = NULL WHERE id = ?",
                (repo_id,),
            )


def record_verify(repo_id: int, ok: bool, message: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE repos SET last_verify_at = ?, last_verify_status = ?, "
            "last_verify_message = ? WHERE id = ?",
            (time.time(), "ok" if ok else "error", message, repo_id),
        )


SYNC_LOG_KEEP = 200


def add_sync_log(repo_id: int, job_id: int | None, started_at: float, finished_at: float,
                 action: str, status: str, message: str, size_bytes: int | None) -> None:
    """Add one result row for a repository and drop its rows beyond the newest 200."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO repo_sync_log (repo_id, job_id, started_at, finished_at, action, "
            "status, message, size_bytes, duration) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (repo_id, job_id, started_at, finished_at, action, status, message,
             size_bytes, finished_at - started_at),
        )
        conn.execute(
            "DELETE FROM repo_sync_log WHERE repo_id = ? AND id NOT IN ("
            "SELECT id FROM repo_sync_log WHERE repo_id = ? ORDER BY id DESC LIMIT ?)",
            (repo_id, repo_id, SYNC_LOG_KEEP),
        )


def repo_sync_log(repo_id: int, limit: int = 50) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM repo_sync_log WHERE repo_id = ? ORDER BY id DESC LIMIT ?",
            (repo_id, limit),
        ).fetchall()


def job_repo_results(job_id: int) -> list[sqlite3.Row]:
    """Return the per-repository rows of one job, the failed ones first."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT l.*, r.full_name FROM repo_sync_log l "
            "LEFT JOIN repos r ON r.id = l.repo_id WHERE l.job_id = ? "
            "ORDER BY CASE l.status WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, "
            "r.full_name COLLATE NOCASE",
            (job_id,),
        ).fetchall()


def add_history_snapshot(job_id: int, repo_count: int, ok: int, fail: int,
                         total_bytes: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sync_history (job_id, recorded_at, repo_count, ok_count, "
            "fail_count, total_bytes) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, time.time(), repo_count, ok, fail, total_bytes),
        )


def history_snapshots(limit: int = 90) -> list[sqlite3.Row]:
    """Return the newest snapshots, oldest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_history ORDER BY recorded_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return rows[::-1]


def update_job_progress(job_id: int, ok: int, fail: int, detail: str) -> None:
    """Store the counts and the log so far of a job that still runs."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE job_runs SET ok_count = ?, fail_count = ?, detail = ? "
            "WHERE id = ? AND status = 'running'",
            (ok, fail, detail, job_id),
        )


def mark_interrupted_jobs() -> int:
    """Close every job run that a restart left as 'running'. Returns the count.

    Call it at startup only, before the scheduler starts: at that time no job
    of this process can be running.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE job_runs SET status = 'interrupted', finished_at = ?, "
            "summary = 'Interrupted by a restart' WHERE status = 'running'",
            (time.time(),),
        )
        return cur.rowcount


def running_jobs() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM job_runs WHERE status = 'running' ORDER BY started_at"
        ).fetchall()


def max_job_id() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM job_runs").fetchone()[0]
