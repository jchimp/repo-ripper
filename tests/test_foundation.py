"""Auth, CSRF, health, migrations and settings-secret behaviour."""
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, db, migrations
from app.config import Settings

from .conftest import TEST_USER, csrf_from


def test_health_is_public(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/", "/repos", "/settings", "/jobs", "/jobs/1"])
def test_pages_need_a_session(client: TestClient, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_private_post_needs_a_session(client: TestClient) -> None:
    token = csrf_from(client.get("/login").text)
    resp = client.post("/run/scan", headers={"X-CSRF-Token": token})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_static_assets_are_served(client: TestClient) -> None:
    for name in ("htmx.min.js", "theme.js", "style.css"):
        assert client.get(f"/static/{name}").status_code == 200, name


def test_login_and_logout(authed: tuple[TestClient, str]) -> None:
    client, token = authed
    assert client.get("/").status_code == 200
    resp = client.post("/logout", data={"csrf_token": token})
    assert resp.status_code == 303
    assert client.get("/").status_code == 303


def test_login_rejects_wrong_password(client: TestClient) -> None:
    token = csrf_from(client.get("/login").text)
    resp = client.post("/login", data={
        "username": TEST_USER, "password": "wrong", "csrf_token": token})
    assert resp.status_code == 401
    assert client.get("/").status_code == 303


def test_login_without_csrf_is_refused(client: TestClient) -> None:
    client.get("/login")
    resp = client.post("/login", data={"username": TEST_USER, "password": "x"})
    assert resp.status_code == 403


def test_post_without_csrf_is_refused(authed: tuple[TestClient, str]) -> None:
    client, token = authed
    assert client.post("/settings", data={"schedule_scan": "0 1 * * *"}).status_code == 403
    assert client.post("/settings", data={"csrf_token": "wrong"}).status_code == 403
    assert db.get_setting("schedule_scan") == "0 3 * * *"
    # Header form, as HTMX sends it.
    resp = client.post("/settings", data={"schedule_scan": "0 1 * * *"},
                       headers={"X-CSRF-Token": token})
    assert resp.status_code == 303
    assert db.get_setting("schedule_scan") == "0 1 * * *"


def test_login_throttle(client: TestClient) -> None:
    token = csrf_from(client.get("/login").text)
    bad = {"username": TEST_USER, "password": "wrong", "csrf_token": token}
    for _ in range(auth.MAX_FAILS):
        assert client.post("/login", data=bad).status_code == 401
    assert client.post("/login", data=bad).status_code == 429
    # The right password is refused too while the address is locked out.
    from .conftest import TEST_PASSWORD
    good = {"username": TEST_USER, "password": TEST_PASSWORD, "csrf_token": token}
    assert client.post("/login", data=good).status_code == 429


def test_secret_setting_is_not_echoed(authed: tuple[TestClient, str]) -> None:
    client, token = authed
    secret = "123456:SECRET-BOT-TOKEN"
    client.post("/settings", data={"csrf_token": token, "telegram_bot_token": secret})
    assert db.get_setting("telegram_bot_token") == secret
    assert secret not in client.get("/settings").text
    # Empty field keeps the stored value.
    client.post("/settings", data={"csrf_token": token, "telegram_bot_token": ""})
    assert db.get_setting("telegram_bot_token") == secret
    # Explicit clear removes it.
    client.post("/settings", data={"csrf_token": token, "telegram_bot_token_clear": "on"})
    assert db.get_setting("telegram_bot_token") == ""


def test_placeholder_config_is_detected() -> None:
    bad = Settings(_env_file=None, admin_password="change-me-now",
                   secret_key="replace-with-a-long-random-hex-string")
    assert len(bad.insecure_defaults()) == 2
    short = Settings(_env_file=None, admin_password="fine-password", secret_key="short")
    assert len(short.insecure_defaults()) == 1
    good = Settings(_env_file=None, admin_password="fine-password", secret_key="k" * 32)
    assert good.insecure_defaults() == []


def test_migrations_upgrade_a_pre_migration_database(tmp_path: Path) -> None:
    """A database made by the first release gets the new columns, data kept."""
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.executescript((migrations.MIGRATIONS_DIR / "0001_initial.sql").read_text())
    conn.execute("INSERT INTO repos (full_name) VALUES ('octo/old')")
    conn.commit()

    applied = migrations.apply_all(conn)
    assert applied[0] == "0001_initial.sql"
    row = conn.execute("SELECT full_name, source, metadata_enabled FROM repos").fetchone()
    assert row == ("octo/old", "mine", 1)
    # Second run is a no-op.
    assert migrations.apply_all(conn) == []
    conn.close()


def test_failed_migration_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mig_dir = tmp_path / "migs"
    mig_dir.mkdir()
    (mig_dir / "0001_bad.sql").write_text("CREATE TABLE t (id INTEGER);\nNOT VALID SQL;")
    monkeypatch.setattr(migrations, "MIGRATIONS_DIR", mig_dir)
    conn = sqlite3.connect(tmp_path / "x.db")
    with pytest.raises(RuntimeError, match="0001_bad.sql"):
        migrations.apply_all(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "t" not in tables
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
    conn.close()


def test_job_registry_drives_dashboard_and_settings(authed: tuple[TestClient, str]) -> None:
    from app import jobs, scheduler
    client, token = authed
    dash = client.get("/").text
    settings_html = client.get("/settings").text
    for spec in jobs.JOBS.values():
        assert spec.label in dash
        assert f'name="{spec.schedule_key}"' in settings_html
    # An empty schedule removes the job from the scheduler: manual only.
    client.post("/settings", data={"csrf_token": token, "schedule_scan": ""})
    assert scheduler.next_runs()["scan"] is None
    client.post("/settings", data={"csrf_token": token, "schedule_scan": "0 4 * * *"})
    assert scheduler.next_runs()["scan"] is not None


def test_static_urls_carry_a_content_version(client: TestClient) -> None:
    """A changed stylesheet gets a new URL, so no browser shows an old copy."""
    import re

    from app import web
    html = client.get("/login").text
    match = re.search(r'href="(/static/style\.css\?v=[0-9a-f]{10})"', html)
    assert match, html
    assert client.get(match.group(1)).status_code == 200
    assert web.static_url("no-such-file.css") == "/static/no-such-file.css"


def test_version_shows_in_the_footer(authed: tuple[TestClient, str]) -> None:
    import app as app_package
    client, _token = authed
    assert app_package.__version__ == "0.2.0"
    assert f"repo-ripper {app_package.__version__}</footer>" in client.get("/settings").text
    # Not on the sign-in page: a visitor without a session learns nothing.
    client.cookies.clear()
    assert "app-footer" not in client.get("/login").text


def test_old_job_runs_are_dropped(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "JOB_RUNS_KEEP", 3)
    still_running = db.start_job("pull")
    for _ in range(5):
        db.finish_job(db.start_job("scan"), "ok", 0, 0, "done", "")
    newest = db.start_job("scan")
    ids = [r["id"] for r in db.recent_jobs(50)]
    # The newest three stay. A run that is in work is never dropped.
    assert sorted(ids) == [still_running, newest - 2, newest - 1, newest]
