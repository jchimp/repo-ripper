"""Release files: download, limits, kept copies, the job, the pages."""
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.metadata import assets, assets_job, capture, store
from app.metadata import client as gh_client

from .fake_github import FakeGitHub, FakeRepo

DEMO = "octo/demo"
MB = 1024 * 1024


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHub]:
    """A fake GitHub with one repository and a mirror root in a temp folder."""
    monkeypatch.setenv("MIRROR_ROOT", str(tmp_path / "mirrors"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app.config import get_settings
    get_settings.cache_clear()
    db.init_db()
    gh = FakeGitHub()
    repo = gh.add_repo(DEMO)
    repo.releases.clear()
    monkeypatch.setattr(gh_client, "_transport", gh.transport())
    monkeypatch.setattr(gh_client, "_sleep", lambda seconds: None)
    monkeypatch.setattr(os, "fsync", lambda fd: None)
    yield gh
    get_settings.cache_clear()


def demo(fake: FakeGitHub) -> FakeRepo:
    return fake.repos[DEMO]


def run(keep: int = 10, max_bytes: int = 10 * MB, full_name: str = DEMO) -> assets.AssetsResult:
    """Capture the metadata (this writes releases.json), then sync the files."""
    with gh_client.GitHubClient() as client:
        capture.capture_repo(client, full_name)
        return assets.sync_repo(client, full_name, keep_releases=keep, max_bytes=max_bytes)


def on_disk(full_name: str = DEMO) -> set[str]:
    base = assets.assets_dir(full_name)
    return {p.relative_to(base).as_posix() for p in base.rglob("*")
            if p.is_file() and p.name != "index.json"}


def test_downloads_attached_files_and_source_archives(fake: FakeGitHub) -> None:
    demo(fake).add_release(7, "v1.0", "2024-03-01T00:00:00Z",
                           {"app.zip": b"zip-bytes", "setup.exe": b"exe-bytes"})
    res = run()
    assert res.status == "ok"
    assert (res.downloaded, res.files) == (4, 4)
    assert on_disk() == {"7/files/app.zip", "7/files/setup.exe",
                         "7/source/demo-v1.0.zip", "7/source/demo-v1.0.tar.gz"}
    base = assets.assets_dir(DEMO)
    assert (base / "7/files/app.zip").read_bytes() == b"zip-bytes"
    assert (base / "7/source/demo-v1.0.zip").read_bytes() == b"zip of v1.0"
    entry = assets.load_index(DEMO)["releases"]["7"]["files"]["700"]
    assert entry["state"] == "stored" and entry["verified"] is True
    assert not list(base.rglob("*.tmp"))


def test_second_run_downloads_nothing(fake: FakeGitHub) -> None:
    demo(fake).add_release(7, "v1.0", "2024-03-01T00:00:00Z", {"app.zip": b"zip-bytes"})
    run()
    before = len(fake.paths("releases/assets")) + len(fake.paths("ball/"))
    res = run()
    assert res.downloaded == 0 and res.files == 3
    assert len(fake.paths("releases/assets")) + len(fake.paths("ball/")) == before


def test_changed_file_is_downloaded_again(fake: FakeGitHub) -> None:
    rel = demo(fake).add_release(7, "v1.0", "2024-03-01T00:00:00Z", {"app.zip": b"old"})
    run()
    demo(fake).asset_bytes[700] = b"new-content"
    rel["assets"][0].update({"size": len(b"new-content"), "updated_at": "2024-04-01T00:00:00Z"})
    del rel["assets"][0]["digest"]
    res = run()
    assert res.downloaded == 1
    assert (assets.assets_dir(DEMO) / "7/files/app.zip").read_bytes() == b"new-content"


def test_only_the_newest_releases_get_downloads(fake: FakeGitHub) -> None:
    for i in range(1, 5):
        demo(fake).add_release(i, f"v{i}", f"2024-0{i}-01T00:00:00Z", {"a.bin": b"x" * i})
    run(keep=2)
    assert {p.split("/")[0] for p in on_disk()} == {"3", "4"}
    # 0 = all releases.
    run(keep=0)
    assert {p.split("/")[0] for p in on_disk()} == {"1", "2", "3", "4"}


def test_file_larger_than_the_limit_is_not_stored(fake: FakeGitHub) -> None:
    demo(fake).add_release(7, "v1.0", "2024-03-01T00:00:00Z",
                           {"big.iso": b"x" * 2000, "small.txt": b"ok"})
    # The listed size stops the big attached file before any request.
    res = run(max_bytes=1000)
    assert res.status == "ok" and "larger than the size limit" in res.message
    files = assets.load_index(DEMO)["releases"]["7"]["files"]
    assert files["700"]["state"] == "too_large" and files["701"]["state"] == "stored"
    assert "7/files/big.iso" not in on_disk()
    assert not [p for p in fake.paths("releases/assets") if p.endswith("/700")]


def test_source_archive_over_the_limit_stops_in_the_stream(fake: FakeGitHub) -> None:
    demo(fake).add_release(7, "v1.0", "2024-03-01T00:00:00Z")
    demo(fake).archives["zipball/v1.0"] = b"z" * 5000
    run(max_bytes=1000)
    source = assets.load_index(DEMO)["releases"]["7"]["source"]
    assert source["zip"]["state"] == "too_large" and source["tar"]["state"] == "stored"
    assert on_disk() == {"7/source/demo-v1.0.tar.gz"}
    # Not tried again with the same limit; tried again when the limit is raised.
    before = len(fake.paths("zipball"))
    run(max_bytes=1000)
    assert len(fake.paths("zipball")) == before
    run(max_bytes=10000)
    assert "7/source/demo-v1.0.zip" in on_disk()


def test_wrong_size_or_checksum_is_refused(fake: FakeGitHub) -> None:
    rel = demo(fake).add_release(7, "v1.0", "2024-03-01T00:00:00Z",
                                 {"a.bin": b"abc", "b.bin": b"abc"})
    rel["assets"][0]["size"] = 99                      # GitHub lists a different size
    rel["assets"][1]["digest"] = "sha256:" + "0" * 64  # and a different checksum
    res = run()
    assert res.status == "partial"
    files = assets.load_index(DEMO)["releases"]["7"]["files"]
    assert files["700"]["state"] == "failed" and "Size is 3 bytes" in files["700"]["error"]
    assert files["701"]["state"] == "failed" and "checksum" in files["701"]["error"]
    assert not [p for p in on_disk() if "/files/" in p]
    assert not list(assets.assets_dir(DEMO).rglob("*.tmp"))


def test_removed_release_and_file_keep_their_copies(fake: FakeGitHub) -> None:
    repo = demo(fake)
    repo.add_release(7, "v1.0", "2024-03-01T00:00:00Z", {"a.bin": b"a", "b.bin": b"b"})
    repo.add_release(8, "v2.0", "2024-04-01T00:00:00Z", {"c.bin": b"c"})
    run()
    stored = on_disk()
    repo.releases[:] = [r for r in repo.releases if r["id"] != 8]   # release deleted
    repo.releases[0]["assets"].pop()                                  # file b.bin removed
    res = run()
    assert res.status == "ok"
    assert on_disk() == stored
    index = assets.load_index(DEMO)["releases"]
    assert index["8"]["removed_upstream"] is True
    assert index["7"]["removed_upstream"] is False
    assert index["7"]["files"]["701"]["removed_upstream"] is True
    assert index["7"]["files"]["700"]["removed_upstream"] is False


def test_rate_limit_stops_and_the_next_run_continues(fake: FakeGitHub) -> None:
    demo(fake).add_release(7, "v1.0", "2024-03-01T00:00:00Z",
                           {"a.bin": b"a", "b.bin": b"b", "c.bin": b"c"})
    with gh_client.GitHubClient() as client:
        capture.capture_repo(client, DEMO)
    fake.budget = 2
    with gh_client.GitHubClient() as client:
        res = assets.sync_repo(client, DEMO, keep_releases=10, max_bytes=MB)
    assert res.status == "rate_limited" and res.downloaded == 2
    fake.budget = None
    with gh_client.GitHubClient() as client:
        res = assets.sync_repo(client, DEMO, keep_releases=10, max_bytes=MB)
    assert res.status == "ok" and res.downloaded == 3 and res.files == 5


def test_disk_with_too_little_space_stops(fake: FakeGitHub,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    demo(fake).add_release(7, "v1.0", "2024-03-01T00:00:00Z", {"a.bin": b"a"})
    usage = type("Usage", (), {"free": 10 * MB, "total": 100 * MB, "used": 90 * MB})()
    monkeypatch.setattr(assets.shutil, "disk_usage", lambda path: usage)
    res = run()
    assert res.status == "error" and "The disk has 10 MB free" in res.message
    assert on_disk() == set()


def test_hostile_names_stay_inside_the_folder(fake: FakeGitHub) -> None:
    demo(fake).add_release(7, "feature/../../x", "2024-03-01T00:00:00Z",
                           {"../../evil.sh": b"1", "..": b"2", "a b(1).zip": b"3"})
    run()
    base = assets.assets_dir(DEMO).resolve()
    for p in base.rglob("*"):
        assert p.resolve().is_relative_to(base)
    names = {p.rsplit("/", 1)[-1] for p in on_disk() if "/files/" in p}
    assert names == {"evil.sh", "asset-701", "a b(1).zip"}
    assert assets.safe_name("..", "x") == "x"
    assert assets.safe_name("a/b\\c.txt", "x") == "c.txt"


def test_download_url_on_another_host_is_refused(fake: FakeGitHub, tmp_path: Path) -> None:
    with gh_client.GitHubClient() as client:
        with pytest.raises(gh_client.GitHubError, match="different host"):
            client.download("https://evil.example/file", tmp_path / "x")
    assert not (tmp_path / "x").exists()


def test_stored_path_refuses_entries_that_point_outside(fake: FakeGitHub) -> None:
    demo(fake).add_release(7, "v1.0", "2024-03-01T00:00:00Z", {"a.bin": b"a"})
    run()
    assert assets.stored_path(DEMO, "7", "files", "700").name == "a.bin"
    assert assets.stored_path(DEMO, "7", "other", "700") is None
    assert assets.stored_path(DEMO, "9", "files", "700") is None
    index = assets.load_index(DEMO)
    index["releases"]["7"]["files"]["700"]["path"] = "../index.json"
    store.atomic_write_json(assets.assets_dir(DEMO) / "index.json", index)
    assert assets.stored_path(DEMO, "7", "files", "700") is None


# --- job, switch, pages -------------------------------------------------------

@pytest.fixture
def app_fake(client: TestClient, fake: FakeGitHub) -> tuple[TestClient, FakeGitHub]:
    """The fake GitHub plus a started app."""
    return client, fake


def enrol(name: str) -> int:
    db.upsert_repo({"full_name": name, "private": 0, "fork": 0, "archived": 0,
                    "default_branch": "main", "mirror_path": f"{name}.git",
                    "discovered_at": time.time()})
    return db.get_repo_by_name(name)["id"]


def sign_in(client: TestClient) -> str:
    from .conftest import login
    return login(client)


def test_switch_is_off_by_default_and_the_job_skips(app_fake) -> None:
    _client, fake = app_fake
    demo(fake).add_release(7, "v1.0", "2024-03-01T00:00:00Z", {"a.bin": b"a"})
    rid = enrol(DEMO)
    assert db.get_repo(rid)["assets_enabled"] == 0
    assets_job.run_assets_job()
    job = db.recent_jobs(1)[0]
    assert job["job_type"] == "assets" and job["status"] == "skipped"
    assert not fake.paths("releases/assets")


def test_job_downloads_for_repositories_with_the_switch_on(app_fake) -> None:
    _client, fake = app_fake
    demo(fake).add_release(7, "v1.0", "2024-03-01T00:00:00Z", {"a.bin": b"abc"})
    other = fake.add_repo("octo/off")
    other.releases.clear()
    other.add_release(9, "v9", "2024-03-01T00:00:00Z", {"z.bin": b"z"})
    rid = enrol(DEMO)
    enrol("octo/off")
    db.toggle_repo_assets(rid)
    with gh_client.GitHubClient() as c:
        capture.capture_repo(c, DEMO)
        capture.capture_repo(c, "octo/off")

    assets_job.run_assets_job()
    job = db.recent_jobs(1)[0]
    assert job["status"] == "ok", job["summary"]
    assert "3 files downloaded" in job["summary"]
    row = db.get_repo(rid)
    assert (row["assets_status"], row["assets_files"]) == ("ok", 3)
    assert row["assets_bytes"] == 3 + len(b"zip of v1.0") + len(b"tar of v1.0")
    assert on_disk("octo/off") == set()


def test_settings_store_the_limits(app_fake) -> None:
    client, _fake = app_fake
    token = sign_in(client)
    page = client.get("/settings").text
    assert 'name="assets_keep_releases"' in page and 'value="10"' in page
    assert 'name="assets_max_mb"' in page and 'value="2048"' in page
    client.post("/settings", data={"csrf_token": token, "assets_keep_releases": "3",
                                   "assets_max_mb": "1024"})
    assert (assets_job.keep_releases(), assets_job.max_mb()) == (3, 1024)
    client.post("/settings", data={"csrf_token": token, "assets_keep_releases": "abc",
                                   "assets_max_mb": "0"})
    assert (assets_job.keep_releases(), assets_job.max_mb()) == (3, 1)


def test_reminder_shows_until_one_repository_has_the_switch_on(app_fake) -> None:
    client, _fake = app_fake
    token = sign_in(client)
    reminder = "Release files are not downloaded."
    assert reminder not in client.get("/").text        # no repositories yet
    rid = enrol(DEMO)
    assert reminder in client.get("/").text
    resp = client.post(f"/repos/{rid}/assets-toggle", headers={"X-CSRF-Token": token,
                                                               "HX-Request": "true"})
    assert resp.status_code == 200 and 'aria-label="Download release files' in resp.text
    assert db.get_repo(rid)["assets_enabled"] == 1
    assert reminder not in client.get("/").text


def test_releases_page_and_file_download(app_fake) -> None:
    client, fake = app_fake
    token = sign_in(client)
    demo(fake).add_release(7, "v1.0", "2024-03-01T00:00:00Z",
                           {"app.zip": b"zip-bytes", "big.iso": b"x" * 3000})
    rid = enrol(DEMO)
    with gh_client.GitHubClient() as c:
        capture.capture_repo(c, DEMO)

    page = client.get(f"/repos/{rid}/releases").text
    assert "Release files are not downloaded for this repository." in page
    assert "not downloaded</span>" in page and "/files/700" not in page

    resp = client.post(f"/repos/{rid}/assets-toggle",
                       data={"csrf_token": token, "back": "releases"})
    assert resp.headers["location"] == f"/repos/{rid}/releases"
    with gh_client.GitHubClient() as c:
        assets.sync_repo(c, DEMO, keep_releases=10, max_bytes=1000)

    page = client.get(f"/repos/{rid}/releases").text
    assert "Release files are not downloaded for this repository." not in page
    assert f'href="/repos/{rid}/releases/7/files/700"' in page
    assert "stored, checksum ok" in page and "too large, not stored" in page
    assert f'href="/repos/{rid}/releases/7/source/zip"' in page

    got = client.get(f"/repos/{rid}/releases/7/files/700")
    assert got.status_code == 200 and got.content == b"zip-bytes"
    assert got.headers["content-type"] == "application/octet-stream"
    assert "attachment" in got.headers["content-disposition"]
    assert got.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"/repos/{rid}/releases/7/source/tar").content == b"tar of v1.0"
    for bad in ("7/files/701", "7/files/999", "8/files/700", "7/other/700"):
        assert client.get(f"/repos/{rid}/releases/{bad}").status_code == 404, bad


def test_file_download_needs_a_session(app_fake) -> None:
    client, _fake = app_fake
    resp = client.get("/repos/1/releases/7/files/700")
    assert resp.status_code == 303 and resp.headers["location"] == "/login"


def test_transient_download_error_is_retried(fake: FakeGitHub) -> None:
    demo(fake).add_release(7, "v1.0", "2024-03-01T00:00:00Z", {"a.bin": b"abc"})
    fake.forced[f"/repos/{DEMO}/releases/assets/700"] = [(503, {}, {"message": "busy"})]
    res = run()
    assert res.status == "ok" and "7/files/a.bin" in on_disk()

