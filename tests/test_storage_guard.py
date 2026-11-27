"""Storage marker, protection copy guards, crash guards and the scan name check.

No network and no rsync: the protection copy is replaced, or `subprocess.run`
is, so the tests also run on Windows.
"""
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import (
    db,
    github,
    jobs,
    notify,
    protection,
    runtime,
    setup_state,
    storage_guard,
    watch,
)
from app.config import Settings
from app.metadata import assets_job
from app.metadata import job as meta_job

MARKER = ".repo-ripper-root"


@pytest.fixture
def sent(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool, str]]:
    """An app with a database, plus the notifications that the jobs send."""
    calls: list[tuple[str, bool, str]] = []
    monkeypatch.setattr(notify, "notify_job",
                        lambda job_type, ok, summary: calls.append((job_type, ok, summary)))
    return calls


@pytest.fixture
def copies(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Replace rsync. Holds the `delete` value of each protection copy that ran."""
    calls: list[bool] = []
    monkeypatch.setattr(jobs.protection, "run_protection_copy",
                        lambda delete: calls.append(delete) or (True, "copied"))
    return calls


def add_repo(full_name: str, **fields: object) -> int:
    db.upsert_repo({"full_name": full_name, "private": 0, "fork": 0, "archived": 0,
                    "default_branch": "main", "mirror_path": f"{full_name}.git",
                    "discovered_at": time.time()})
    with db.get_conn() as conn:
        for key, value in fields.items():
            conn.execute(f"UPDATE repos SET {key} = ? WHERE full_name = ?", (value, full_name))
        return conn.execute("SELECT id FROM repos WHERE full_name = ?",
                            (full_name,)).fetchone()[0]


def fake_mirror(full_name: str) -> None:
    """Put the one file on disk that says "this mirror exists"."""
    folder = runtime.mirror_root() / f"{full_name}.git"
    folder.mkdir(parents=True)
    (folder / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


def last_job() -> sqlite3.Row:
    # By id, not by start time: two runs can start in the same clock tick.
    job = db.get_job(db.max_job_id())
    assert job is not None
    return job


def unmount(folder: Path) -> None:
    """Make the folder look like a mount point whose share is not mounted."""
    shutil.rmtree(folder)


# --- H-1: the marker file ---------------------------------------------------------

def test_selecting_the_folders_writes_the_marker(client: TestClient, tmp_path: Path) -> None:
    mirror, copy = tmp_path / "nas" / "git", tmp_path / "nas2" / "copy"
    assert setup_state.save_storage(str(mirror), str(copy), False) == {}
    assert "repo-ripper" in (mirror / MARKER).read_text(encoding="utf-8")
    assert (copy / MARKER).is_file()
    assert [p.name for p in mirror.iterdir()] == [MARKER]     # no temporary file stays
    assert storage_guard.mirror_problem() is None
    assert storage_guard.protection_problem() is None


def test_no_protection_copy_writes_only_the_mirror_marker(client: TestClient,
                                                           tmp_path: Path) -> None:
    mirror, copy = tmp_path / "git", tmp_path / "copy"
    assert setup_state.save_storage(str(mirror), str(copy), True) == {}
    assert (mirror / MARKER).is_file()
    assert not copy.exists()


def test_a_marker_that_cannot_be_written_stores_nothing(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(folder: Path) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(storage_guard, "write_marker", fail)
    errors = setup_state.save_storage(str(tmp_path / "git"), str(tmp_path / "copy"), False)
    assert MARKER in errors["mirror_root"] and "Permission denied" in errors["mirror_root"]
    assert db.get_setting("mirror_root") is None


def test_problem_text_names_the_folder_and_the_fix(client: TestClient) -> None:
    root = runtime.mirror_root()
    assert storage_guard.mirror_problem() is None
    (root / MARKER).unlink()
    text = storage_guard.mirror_problem()
    assert text is not None
    assert str(root) in text and MARKER in text
    assert "not mounted" in text and "Settings, Storage" in text
    # The second folder is judged by its own marker.
    assert storage_guard.protection_problem() is None


@pytest.mark.parametrize("key", ["pull", "verify", "metadata", "assets"])
def test_job_stops_when_the_mirror_folder_is_not_mounted(sent, key: str) -> None:
    add_repo("octo/alpha", assets_enabled=1)
    root = runtime.mirror_root()
    unmount(root)
    run = {"pull": jobs.run_pull_job, "verify": jobs.run_verify_job,
           "metadata": meta_job.run_metadata_job, "assets": assets_job.run_assets_job}[key]
    run()
    job = last_job()
    assert (job["job_type"], job["status"]) == (key, "error")
    assert MARKER in job["summary"] and "not mounted" in job["summary"]
    assert sent == [(key, False, job["summary"])]
    # The job must not make the folder: on a real host that is the local disk.
    assert not root.exists()
    assert db.running_jobs() == []
    assert not jobs.is_running(key)


def test_selected_sync_is_refused_when_the_mirror_folder_is_not_mounted(sent) -> None:
    rid = add_repo("octo/alpha")
    unmount(runtime.mirror_root())
    before = db.max_job_id()
    with pytest.raises(jobs.SyncRefused, match="not mounted"):
        jobs.prepare_selected_sync([rid])
    assert db.max_job_id() == before          # no job run was opened
    assert jobs._claim("octo/alpha")          # and the repository was not reserved
    jobs._release("octo/alpha")


# --- H-1: installs from before the marker ---------------------------------------

def test_adoption_marks_folders_that_hold_data(client: TestClient) -> None:
    mirror, copy = runtime.mirror_root(), runtime.protection_root()
    (mirror / MARKER).unlink()
    (copy / MARKER).unlink()
    fake_mirror("octo/alpha")
    (copy / "octo").mkdir()
    db.set_setting(storage_guard.ADOPTED_KEY, "0")
    storage_guard.adopt_existing_folders()
    assert (mirror / MARKER).is_file() and (copy / MARKER).is_file()
    assert db.get_setting(storage_guard.ADOPTED_KEY) == "1"


def test_adoption_does_not_mark_empty_or_missing_folders(client: TestClient) -> None:
    mirror, copy = runtime.mirror_root(), runtime.protection_root()
    (mirror / MARKER).unlink()
    # A folder with no mirror in it is not proof that this is the NAS share.
    (mirror / "octo").mkdir()
    (mirror / "notes.txt").write_text("x", encoding="utf-8")
    unmount(copy)
    db.set_setting(storage_guard.ADOPTED_KEY, "0")
    storage_guard.adopt_existing_folders()
    assert not (mirror / MARKER).exists() and not copy.exists()
    assert db.get_setting(storage_guard.ADOPTED_KEY) == "1"

    # One time only: data that comes later does not get the marker by itself.
    fake_mirror("octo/alpha")
    storage_guard.adopt_existing_folders()
    assert not (mirror / MARKER).exists()


def test_adoption_tries_again_after_a_write_error(client: TestClient,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    mirror = runtime.mirror_root()
    (mirror / MARKER).unlink()
    fake_mirror("octo/alpha")
    db.set_setting(storage_guard.ADOPTED_KEY, "0")

    def fail(folder: Path) -> None:
        raise OSError("read-only share")

    with monkeypatch.context() as patch:
        patch.setattr(storage_guard, "write_marker", fail)
        storage_guard.adopt_existing_folders()                 # must not raise
    assert db.get_setting(storage_guard.ADOPTED_KEY) == "0"
    storage_guard.adopt_existing_folders()
    assert (mirror / MARKER).is_file()


# --- C-1: protection copy guards --------------------------------------------------

def test_delete_is_off_by_default() -> None:
    assert Settings.model_fields["protection_delete"].default is False


@pytest.mark.parametrize("which", ["mirror", "protection"])
def test_protection_job_refuses_a_folder_without_marker(sent, copies, which: str) -> None:
    folder = runtime.mirror_root() if which == "mirror" else runtime.protection_root()
    (folder / MARKER).unlink()
    jobs.run_protection_job()
    job = last_job()
    assert job["status"] == "error"
    assert f"The {which} folder {folder}" in job["summary"]
    assert "The protection copy did not run." in job["summary"]
    assert copies == []
    assert sent == [("protection", False, job["summary"])]


def test_delete_is_refused_when_the_mirror_folder_lost_most_repositories(sent, copies) -> None:
    now = time.time()
    for name in ("octo/a", "octo/b", "octo/c"):
        add_repo(name, last_ok_at=now)
    add_repo("octo/never")                   # no good sync: not expected on disk
    fake_mirror("octo/a")
    db.set_setting("protection_delete", "1")
    jobs.run_protection_job()
    job = last_job()
    assert job["status"] == "error"
    assert "has 1 of the 3 repositories" in job["summary"]
    assert "remove the 2 missing repositories" in job["summary"]
    assert copies == []
    assert sent == [("protection", False, job["summary"])]

    # Half or more on disk is normal (a repository can be moved or removed by hand).
    fake_mirror("octo/b")
    jobs.run_protection_job()
    assert last_job()["status"] == "ok"
    assert copies == [True]


def test_without_delete_the_repository_count_is_not_examined(sent, copies) -> None:
    add_repo("octo/a", last_ok_at=time.time())
    db.set_setting("protection_delete", "0")
    jobs.run_protection_job()
    assert last_job()["status"] == "ok"
    assert copies == [False]


def test_rsync_command(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess:
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="sent 1 file\n", stderr="")

    monkeypatch.setattr(protection.subprocess, "run", fake_run)
    copy = runtime.protection_root()
    unmount(copy)
    assert protection.run_protection_copy(delete=False) == (True, "sent 1 file")
    assert protection.run_protection_copy(delete=True)[0] is True
    # The app does not make the protection folder for rsync.
    assert not copy.exists()
    plain, with_delete = commands
    assert plain[0] == "rsync" and "--delete" not in plain
    assert "--delete" in with_delete
    assert not any(arg.startswith("--max-delete") for arg in with_delete)
    # Each folder keeps its own marker.
    assert f"--exclude=/{MARKER}" in plain
    assert plain[-2:] == [runtime.mirror_root().as_posix() + "/", copy.as_posix() + "/"]


# --- H-3: no run stays on "running" ------------------------------------------------

@pytest.mark.parametrize("error", [
    FileNotFoundError(2, "No such file or directory: 'rsync'"),
    PermissionError(13, "Permission denied"),
    subprocess.TimeoutExpired(["rsync"], 1),
])
def test_rsync_that_cannot_run_is_a_result_not_an_exception(
        sent, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess:
        raise error

    monkeypatch.setattr(protection.subprocess, "run", fake_run)
    ok, text = protection.run_protection_copy(delete=False)
    assert ok is False and text.startswith("rsync did not")

    jobs.run_protection_job()
    job = last_job()
    assert (job["status"], job["summary"]) == ("error", "Protection copy failed")
    assert job["detail"] == text
    assert sent == [("protection", False, "Protection copy failed")]
    assert not jobs.is_running("protection")


def test_protection_job_that_raises_is_closed(sent, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(delete: bool) -> tuple[bool, str]:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(jobs.protection, "run_protection_copy", boom)
    jobs.run_protection_job()
    job = last_job()
    assert job["status"] == "error" and "unexpected" in job["summary"]
    assert db.running_jobs() == []
    assert sent == [("protection", False, job["summary"])]
    assert not jobs.is_running("protection")


def test_scan_job_that_raises_is_closed(sent, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> list:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(jobs.setup_state, "refresh_token_check", lambda: None)
    monkeypatch.setattr(db, "list_watch_owners", boom)
    jobs.run_scan_job()
    job = last_job()
    assert (job["job_type"], job["status"]) == ("scan", "error")
    assert "database is locked" in job["summary"]
    assert db.running_jobs() == []
    assert sent == [("scan", False, job["summary"])]
    assert not jobs.is_running("scan")


# --- M-4: not while a job writes to the mirror folder ------------------------------

@pytest.mark.parametrize("key", ["pull", "metadata", "assets"])
def test_protection_job_is_skipped_while_a_writer_runs(sent, copies, key: str) -> None:
    with jobs._LOCKS[key]:
        jobs.run_protection_job()
    job = last_job()
    assert job["status"] == "skipped"
    assert jobs.JOBS[key].label in job["summary"]
    assert "did not run" in job["summary"]
    assert copies == [] and sent == []

    jobs.run_protection_job()
    assert last_job()["status"] == "ok" and len(copies) == 1


def test_protection_job_runs_while_verify_runs(sent, copies) -> None:
    # Verify only reads the mirrors.
    with jobs._LOCKS["verify"]:
        jobs.run_protection_job()
    assert last_job()["status"] == "ok"


# --- L-4: names in the token's own listing ----------------------------------------

def test_scan_skips_a_name_that_is_not_safe_as_a_folder(
        sent, monkeypatch: pytest.MonkeyPatch) -> None:
    def item(gid: int, name: object) -> dict:
        return {"id": gid, "full_name": name, "private": 0, "fork": 0, "archived": 0,
                "default_branch": "main", "has_wiki": False}

    items = [item(1, "octo/good"), item(2, "octo/.."), item(3, "../../etc/passwd"),
             item(4, "octo/a/b"), item(5, None)]
    monkeypatch.setattr(github, "list_all_repos", lambda **_kw: items)
    monkeypatch.setattr(runtime, "github_token", lambda: "test-token")
    monkeypatch.setattr(jobs.setup_state, "refresh_token_check", lambda: None)
    jobs.run_scan_job()

    assert [r["full_name"] for r in db.list_repos()] == ["octo/good"]
    job = last_job()
    assert job["status"] == "ok"
    assert "Discovered 1 repositories (1 new)." in job["summary"]
    assert "Skipped 4 with a name that is not a valid owner/name." in job["summary"]
    assert "[skipped] 'octo/..' — the name is not a valid owner/name" in job["detail"]
    assert "[skipped] '../../etc/passwd'" in job["detail"]
    assert "[ok] my repositories — 1 found, 1 new" in job["detail"]


def test_the_scan_uses_the_check_of_the_watch_module() -> None:
    assert watch.safe_full_name("octo/demo")
    assert not watch.safe_full_name("octo/..")
    assert not watch.safe_full_name("octo")
