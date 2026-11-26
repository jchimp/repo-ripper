"""Schema migrations.

Each `*.sql` file in this folder is applied once, in file-name order, and
recorded in `schema_migrations`. To change the schema, add a new file; never
edit one that has shipped, because existing databases will not re-run it.

File names are `NNNN_short_name.sql`. Number ranges are reserved per feature
area so parallel branches do not collide: 0001-0009 core, 0010-0019 setup,
0020-0029 watched repos, 0030-0039 metadata, 0040-0049 sync engine,
0050-0059 browser.
"""
import re
import sqlite3
import time
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent
_NAME_RE = re.compile(r"^\d{4}_[a-z0-9_]+\.sql$")


def pending(conn: sqlite3.Connection) -> list[Path]:
    """Return migration files that are not yet recorded as applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "name TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
    )
    done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    for f in files:
        if not _NAME_RE.match(f.name):
            raise RuntimeError(f"Bad migration file name: {f.name}")
    return [f for f in files if f.name not in done]


def apply_all(conn: sqlite3.Connection) -> list[str]:
    """Apply every pending migration. Returns the names that were applied.

    Each file runs in its own transaction together with its bookkeeping row,
    so a failed migration leaves the database at the previous version.
    """
    applied: list[str] = []
    for path in pending(conn):
        sql = path.read_text(encoding="utf-8")
        conn.commit()
        try:
            # The name is validated by _NAME_RE, so it is safe to inline here
            # (executescript does not accept parameters).
            conn.executescript(
                f"BEGIN;\n{sql}\n"
                f"INSERT INTO schema_migrations (name, applied_at) "
                f"VALUES ('{path.name}', {time.time()});\nCOMMIT;"
            )
        except sqlite3.Error as exc:
            conn.rollback()
            raise RuntimeError(f"Migration {path.name} failed: {exc}") from exc
        applied.append(path.name)
    return applied
