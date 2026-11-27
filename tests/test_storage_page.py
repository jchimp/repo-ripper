"""Storage page: folders, sizes, protection copy, chart; header order."""
import re
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, runtime, storage_guard


def add_repo(name: str, size: int | None, meta: int | None = None,
             assets: int | None = None) -> None:
    db.upsert_repo({"full_name": name, "private": 0, "fork": 0, "archived": 0,
                    "default_branch": "main", "mirror_path": f"{name}.git",
                    "discovered_at": time.time()})
    with db.get_conn() as conn:
        conn.execute("UPDATE repos SET size_bytes = ?, meta_bytes = ?, assets_bytes = ? "
                     "WHERE full_name = ?", (size, meta, assets, name))


def test_header_order(authed: tuple[TestClient, str]) -> None:
    client, _ = authed
    html = client.get("/storage").text
    nav = re.search(r'<nav class="nav">(.*?)</nav>', html, re.S).group(1)
    assert re.findall(r'href="([^"]+)"', nav) == ["/", "/repos", "/jobs", "/storage",
                                                  "/settings"]
    assert 'href="/storage" class="active"' in nav


def test_page_needs_a_session(client: TestClient) -> None:
    resp = client.get("/storage")
    assert resp.status_code == 303 and resp.headers["location"] == "/login"


def test_empty_install(authed: tuple[TestClient, str]) -> None:
    client, _ = authed
    resp = client.get("/storage")
    assert resp.status_code == 200
    html = resp.text
    assert html.count("marker found") == 2
    assert "used" in html and "free" in html
    assert "The chart needs 2 completed pull jobs. There are 0 now." in html
    assert "No repositories yet." in html
    assert "never" in html and "Run now" in html


def test_missing_marker_and_missing_folder(authed: tuple[TestClient, str],
                                           tmp_path: Path) -> None:
    client, _ = authed
    storage_guard.marker_path(runtime.mirror_root()).unlink()
    shutil.rmtree(runtime.protection_root())
    html = client.get("/storage").text
    assert html.count("marker missing") == 2
    assert "The jobs do not write to this folder." in html
    assert "Disk space not available" in html


def test_sizes_largest_list_and_not_measured(authed: tuple[TestClient, str]) -> None:
    client, _ = authed
    add_repo("octo/big", 3 * 1024**3, meta=None, assets=1024**3)
    add_repo("octo/small", 1024**2, meta=2048)
    add_repo("octo/empty", None)
    html = client.get("/storage").text
    rows = re.findall(r'class="repo-name" href="/repos/\d+">([^<]+)<', html)
    assert rows == ["octo/big", "octo/small"]              # no row without a size
    assert "4.0 GB" in html                                # big: code + release files
    # Metadata: measured for one repository, not for the other two.
    assert "Not measured yet for 2" in html


def test_chart_with_three_series(authed: tuple[TestClient, str]) -> None:
    client, _ = authed
    db.add_history_snapshot(1, 1, 1, 0, 1000)                    # before the migration
    db.add_history_snapshot(2, 1, 1, 0, 2000, meta_bytes=300, assets_bytes=900)
    db.add_history_snapshot(3, 1, 1, 0, 3000, meta_bytes=400, assets_bytes=900)
    html = client.get("/storage").text
    for key in ("code", "meta", "assets"):
        assert f'class="chart-line s-{key}"' in html, key
    assert "3 pull jobs" in html
    # The chart moved: the Dashboard no longer has it.
    assert "chart-line" not in client.get("/").text


@pytest.mark.parametrize("htmx", [False, True])
def test_run_now_returns_to_storage(authed: tuple[TestClient, str],
                                    monkeypatch: pytest.MonkeyPatch, htmx: bool) -> None:
    from app import scheduler
    from app.routes import dashboard
    client, token = authed
    started: list[str] = []
    monkeypatch.setattr(scheduler, "trigger_now", started.append)
    monkeypatch.setattr(dashboard, "_wait_for_start", lambda last_id: None)
    headers = {"X-CSRF-Token": token, **({"HX-Request": "true"} if htmx else {})}
    resp = client.post("/run/protection?back=storage", headers=headers)
    target = resp.headers["HX-Redirect"] if htmx else resp.headers["location"]
    assert target == "/storage" and started == ["protection"]
    # Any other value goes to the Dashboard: the target never comes from the request.
    resp = client.post("/run/protection?back=//evil.example", headers=headers)
    target = resp.headers["HX-Redirect"] if htmx else resp.headers["location"]
    assert target == "/"


def test_pull_job_history_stores_the_three_totals(authed: tuple[TestClient, str]) -> None:
    add_repo("octo/a", 100, meta=10, assets=None)
    add_repo("octo/b", 200, meta=None, assets=None)
    sizes = db.size_totals(db.list_repos())
    assert sizes == {"code": 300, "meta": 10, "assets": None}
