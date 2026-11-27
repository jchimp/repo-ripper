"""Metadata capture: client, incremental sync, store, job and routes.

All GitHub traffic goes to `FakeGitHub` through `httpx.MockTransport`.
"""
import json
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, runtime
from app.metadata import capture, store
from app.metadata import client as gh_client
from app.metadata import job as meta_job

from .fake_github import FakeGitHub, FakeRepo

DEMO = "octo/demo"


def fill_demo(repo: FakeRepo) -> FakeRepo:
    """Three issues and two pull requests, each with its own timestamp."""
    repo.add_issue(1, "Crash on start", "2024-01-05T10:00:00Z", state="closed",
                   comments=3, labels=("bug",))
    repo.add_issue(2, "Add docs", "2024-01-06T10:00:00Z", labels=("docs",))
    repo.add_pull(3, "Fix crash", "2024-01-07T10:00:00Z", state="closed", merged=True,
                  comments=1, review_comments=2, reviews=1, commits=2)
    repo.add_issue(4, "Question", "2024-01-08T10:00:00Z", comments=1)
    repo.add_pull(5, "New feature", "2024-01-09T10:00:00Z", draft=True)
    return repo


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHub]:
    """A fake GitHub wired into the client, and a mirror root in a temp folder."""
    monkeypatch.setenv("MIRROR_ROOT", str(tmp_path / "mirrors"))
    # The token and the storage folders are read through the settings table,
    # so each test needs a database, also the tests that start no app.
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app.config import get_settings
    get_settings.cache_clear()
    db.init_db()
    gh = FakeGitHub()
    fill_demo(gh.add_repo(DEMO))
    monkeypatch.setattr(gh_client, "_transport", gh.transport())
    monkeypatch.setattr(gh_client, "_sleep", lambda seconds: None)
    # fsync is slow on Windows and adds nothing to these tests.
    monkeypatch.setattr(os, "fsync", lambda fd: None)
    # Small pages, so that the tests use the pagination code.
    monkeypatch.setattr(gh_client, "PER_PAGE", 2)
    monkeypatch.setattr(capture, "PER_PAGE", 2)
    yield gh
    get_settings.cache_clear()


def sync(full_name: str = DEMO) -> capture.CaptureResult:
    with gh_client.GitHubClient() as client:
        return capture.capture_repo(client, full_name)


def files_on_disk(full_name: str = DEMO) -> set[tuple[str, int]]:
    base = store.meta_dir(full_name)
    return {(kind, int(p.stem)) for kind, folder in (("issue", "issues"), ("pull", "pulls"))
            for p in (base / folder).glob("*.json")}


def all_files(full_name: str = DEMO) -> list[str]:
    base = store.meta_dir(full_name)
    return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())


# --- capture ----------------------------------------------------------------

def test_first_sync_writes_the_layout(fake: FakeGitHub) -> None:
    res = sync()
    assert (res.status, res.issues, res.pulls, res.fetched) == ("ok", 3, 2, 5)
    assert all_files() == [
        "README.txt", "index.json", "issues/1.json", "issues/2.json", "issues/4.json",
        "labels.json", "milestones.json", "pulls/3.json", "pulls/5.json",
        "releases.json", "repo.json", "state.json",
    ]
    base = store.meta_dir(DEMO)
    assert base == runtime.mirror_root().resolve() / "octo" / "demo.meta"
    assert "format version 1" in (base / "README.txt").read_text(encoding="utf-8")

    issue = store.load_issue(DEMO, 1)
    assert issue["issue"]["title"] == "Crash on start"
    assert [c["body"] for c in issue["comments"]] == [
        "comment 0 on #1", "comment 1 on #1", "comment 2 on #1"]  # 2 pages
    # Raw UTF-8 on disk, not \u escapes.
    assert "ünïcode" in (base / "issues" / "1.json").read_text(encoding="utf-8")

    pull = store.load_pull(DEMO, 3)
    assert pull["pull"]["merged"] is True and pull["pull"]["head"]["ref"] == "feature"
    assert len(pull["comments"]) == 1 and len(pull["review_comments"]) == 2
    assert len(pull["reviews"]) == 1 and len(pull["commits"]) == 2
    commit = pull["commits"][0]
    assert set(commit) == {"sha", "html_url", "commit", "author"}
    assert commit["commit"]["message"] == "Commit 0" and commit["author"] == {"login": "cy"}
    assert pull["fetched_at"] == "2024-06-01T12:00:00Z"
    assert store.load_issue(DEMO, 3) is None  # a pull request is not in issues/

    assert store.load_releases(DEMO)[0]["assets"][0]["name"] == "app.zip"
    assert store.load_repo(DEMO)["full_name"] == DEMO
    assert len(store.load_labels(DEMO)) == 2 and len(store.load_milestones(DEMO)) == 1
    assert "state=all" in next(r for r in fake.requests if "/milestones" in r)

    state = store.load_state(DEMO)
    assert state["cursor"] == "2024-01-09T10:00:00Z"
    assert state["run_in_progress"] is False and state["last_status"] == "ok"
    assert state["counts"] == {"issues": 3, "pulls": 2}
    assert state["format_version"] == 1 and state["failed"] == {}
    # An item with zero comments costs no comments request.
    assert f"/repos/{DEMO}/issues/2/comments" not in fake.paths()
    assert all(a is None for a in fake.auth)  # no token: no Authorization header


def test_second_sync_fetches_only_the_changed_issue(fake: FakeGitHub) -> None:
    sync()
    fake.repos[DEMO].touch(2, "2024-06-02T08:00:00Z", comment="new comment")
    fake.now = datetime(2024, 6, 3, 12, 0, 0, tzinfo=UTC)
    fake.requests.clear()

    res = sync()
    assert (res.status, res.fetched, res.issues, res.pulls) == ("ok", 1, 3, 2)
    item_requests = [p for p in fake.paths() if "/issues/" in p or "/pulls/" in p]
    assert item_requests == [f"/repos/{DEMO}/issues/2/comments"]
    assert "since=2024-01-09T10%3A00%3A00Z" in next(
        r for r in fake.requests if r.startswith(f"/repos/{DEMO}/issues?"))
    assert store.load_issue(DEMO, 2)["comments"][-1]["body"] == "new comment"
    assert store.load_state(DEMO)["cursor"] == "2024-06-02T08:00:00Z"
    entry = next(e for e in store.load_index(DEMO) if e["number"] == 2)
    assert entry["comments"] == 1 and entry["updated_at"] == "2024-06-02T08:00:00Z"

    # Nothing changed: the item at the cursor comes back in the list and is skipped.
    fake.requests.clear()
    res = sync()
    assert (res.fetched, res.skipped) == (0, 1)
    assert [p for p in fake.paths() if "/issues/" in p or "/pulls/" in p] == []


def test_item_changed_in_the_second_of_its_download_is_fetched_again(fake: FakeGitHub) -> None:
    """A skip needs proof that the copy is newer than the change. No proof: download."""
    fake.now = datetime(2024, 1, 9, 10, 0, 0, tzinfo=UTC)  # same second as PR 5's update
    sync()
    fake.repos[DEMO].comments[5].append({"id": 1, "body": "late", "user": {"login": "bob"}})
    fake.now = datetime(2024, 6, 3, tzinfo=UTC)
    res = sync()
    assert res.fetched == 1
    assert store.load_pull(DEMO, 5)["comments"][0]["body"] == "late"


def test_items_that_share_a_timestamp_are_all_captured(fake: FakeGitHub) -> None:
    repo = fake.repos[DEMO]
    for number in range(10, 17):  # 7 items, one timestamp, page size 2
        repo.add_issue(number, f"Bulk {number}", "2024-02-01T00:00:00Z")
    repo.add_issue(20, "After the bulk", "2024-02-02T00:00:00Z")
    res = sync()
    assert res.status == "ok" and res.fetched == 13
    assert {n for _, n in files_on_disk()} == {1, 2, 3, 4, 5, *range(10, 17), 20}
    assert store.load_state(DEMO)["cursor"] == "2024-02-02T00:00:00Z"


def test_rate_limit_stops_cleanly_and_the_next_run_finishes(fake: FakeGitHub) -> None:
    fake.budget = 9
    first = sync()
    assert first.status == "rate_limited" and first.reset_at == fake.reset_at
    assert "rate limit" in first.message
    assert 0 < first.fetched < 5
    # The folder is consistent: the index lists the files, nothing more or less.
    assert {(e["kind"], e["number"]) for e in store.load_index(DEMO)} == files_on_disk()
    assert len(files_on_disk()) == first.fetched
    state = store.load_state(DEMO)
    assert state["last_status"] == "rate_limited" and state["rate_limit_reset_at"]
    assert state["run_in_progress"] is False
    # After the refused request the client sends no more requests.
    refused = len(fake.requests)
    with gh_client.GitHubClient() as client:
        client.remaining, client.reset_at = 0, time.time() + 60
        with pytest.raises(gh_client.RateLimited):
            client.get_json(f"/repos/{DEMO}")
    assert len(fake.requests) == refused

    fake.budget = None
    second = sync()
    assert (second.status, second.issues, second.pulls) == ("ok", 3, 2)
    assert first.fetched + second.fetched == 5  # nothing lost, nothing done twice
    index = store.load_index(DEMO)
    assert [(e["number"], e["kind"]) for e in index] == [
        (1, "issue"), (2, "issue"), (3, "pull"), (4, "issue"), (5, "pull")]
    assert not list(store.meta_dir(DEMO).rglob("*.tmp"))


def test_rate_limit_at_each_request_never_loses_an_item(fake: FakeGitHub) -> None:
    """Stop after 1, 2, 3, ... requests in turn; the end state is always complete."""
    for budget in range(0, 16):
        fake.budget = budget
        res = sync()
        if res.status == "ok":
            break
        assert res.status == "rate_limited"
        assert {(e["kind"], e["number"]) for e in store.load_index(DEMO)} == files_on_disk()
    fake.budget = None
    assert sync().status == "ok"
    assert files_on_disk() == {("issue", 1), ("issue", 2), ("pull", 3), ("issue", 4), ("pull", 5)}
    assert len(store.load_pull(DEMO, 3)["review_comments"]) == 2


def test_secondary_rate_limit_with_retry_after(fake: FakeGitHub) -> None:
    fake.forced[f"/repos/{DEMO}/labels"] = [
        (403, {"Retry-After": "120"}, {"message": "You have exceeded a secondary rate limit."})]
    before = time.time()
    res = sync()
    assert res.status == "rate_limited"
    assert before + 115 < res.reset_at < time.time() + 125
    assert fake.paths().count(f"/repos/{DEMO}/labels") == 1  # no second attempt


def test_transient_errors_are_retried(fake: FakeGitHub) -> None:
    fake.forced[f"/repos/{DEMO}"] = [(503, {}, {}), (502, {}, {})]
    assert sync().status == "ok"
    assert fake.paths().count(f"/repos/{DEMO}") == 3


def test_timeouts_are_retried_then_reported(fake: FakeGitHub,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise httpx.ReadTimeout("timed out", request=request)

    monkeypatch.setattr(gh_client, "_transport", httpx.MockTransport(handler))
    res = sync()
    assert res.status == "error" and "4 attempts" in res.message
    assert len(calls) == 4


def test_token_is_sent_but_never_shown(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "ghp_THIS-IS-A-TEST-SECRET"
    monkeypatch.setattr(runtime, "github_token", lambda: secret)
    fake.always[f"/repos/{DEMO}/pulls/3"] = 500
    res = sync()
    assert set(fake.auth) == {f"Bearer {secret}"}
    assert res.status == "partial"
    on_disk = "".join(p.read_text(encoding="utf-8")
                      for p in store.meta_dir(DEMO).rglob("*") if p.is_file())
    assert secret not in res.message and secret not in on_disk


def test_failed_item_does_not_block_and_is_tried_again(fake: FakeGitHub) -> None:
    fake.always[f"/repos/{DEMO}/pulls/3"] = 500
    res = sync()
    assert res.status == "partial" and "pull/3" in res.message
    assert files_on_disk() == {("issue", 1), ("issue", 2), ("issue", 4), ("pull", 5)}
    state = store.load_state(DEMO)
    assert list(state["failed"]) == ["pull/3"]
    assert state["cursor"] == "2024-01-09T10:00:00Z"  # the cursor moved past the bad item

    del fake.always[f"/repos/{DEMO}/pulls/3"]
    res = sync()
    assert (res.status, res.fetched, res.pulls) == ("ok", 1, 2)
    assert store.load_state(DEMO)["failed"] == {}


def test_github_down_stops_the_repository(fake: FakeGitHub) -> None:
    for number in (1, 4):
        fake.always[f"/repos/{DEMO}/issues/{number}/comments"] = 500
    fake.always[f"/repos/{DEMO}/pulls/3"] = 500
    res = sync()
    assert res.status == "error" and "3 failed items" in res.message
    assert len(store.load_state(DEMO)["failed"]) == 3


def test_issues_disabled_falls_back_to_the_pull_list(fake: FakeGitHub) -> None:
    fake.repos[DEMO].has_issues = False
    res = sync()
    assert (res.status, res.issues, res.pulls) == ("ok", 0, 2)
    state = store.load_state(DEMO)
    assert state["issues_disabled"] is True and state["mode"] == "pulls_only"
    assert state["unavailable"] == {"issues": 410}
    assert files_on_disk() == {("pull", 3), ("pull", 5)}
    # Second run: the list is read again, no pull request is downloaded again.
    fake.now = datetime(2024, 6, 3, tzinfo=UTC)
    assert sync().fetched == 0
    fake.repos[DEMO].touch(5, "2024-06-04T00:00:00Z", comment="hello")
    fake.now = datetime(2024, 6, 5, tzinfo=UTC)
    assert sync().fetched == 1
    assert store.load_pull(DEMO, 5)["comments"][0]["body"] == "hello"


def test_missing_sub_resources_are_not_errors(fake: FakeGitHub) -> None:
    fake.always[f"/repos/{DEMO}/releases"] = 404
    fake.always[f"/repos/{DEMO}/pulls/3/reviews"] = 404
    fake.always[f"/repos/{DEMO}/issues/4/comments"] = 410
    res = sync()
    assert res.status == "ok" and res.fetched == 5
    assert store.load_state(DEMO)["unavailable"] == {
        "releases": 404, "pulls/3/reviews": 404, "issues/4/comments": 410}
    assert store.load_releases(DEMO) == []
    assert store.load_pull(DEMO, 3)["reviews"] == []
    assert len(store.load_pull(DEMO, 3)["review_comments"]) == 2


def test_unknown_repository_is_an_error_with_a_clear_message(fake: FakeGitHub) -> None:
    res = sync("octo/missing")
    assert res.status == "error" and "does not show this repository" in res.message
    assert res.issues == 0 and res.fetched == 0


def test_crash_recovery_rebuilds_the_index_and_removes_temp_files(fake: FakeGitHub) -> None:
    sync()
    base = store.meta_dir(DEMO)
    # Simulate a crash: flag set, index stale, a temp file left behind.
    state = store.load_state(DEMO)
    state["run_in_progress"] = True
    store.atomic_write_json(base / "state.json", state)
    store.write_index(DEMO, [])
    (base / "issues" / ".9.json.deadbeef.tmp").write_text("{", encoding="utf-8")
    fake.now = datetime(2024, 6, 3, tzinfo=UTC)

    res = sync()
    assert (res.status, res.fetched, res.issues, res.pulls) == ("ok", 0, 3, 2)
    assert len(store.load_index(DEMO)) == 5
    assert not list(base.rglob("*.tmp"))


def test_atomic_write_keeps_the_old_file_on_failure(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "sub" / "data.json"
    store.atomic_write_json(target, {"v": 1, "text": "ü"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1, "text": "ü"}
    assert "ü" in target.read_text(encoding="utf-8")
    assert [p.name for p in target.parent.iterdir()] == ["data.json"]

    def broken_replace(src: object, dst: object) -> None:
        raise OSError("disk problem")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk problem"):
        store.atomic_write_json(target, {"v": 2})
    monkeypatch.undo()
    assert json.loads(target.read_text(encoding="utf-8"))["v"] == 1
    assert [p.name for p in target.parent.iterdir()] == ["data.json"]


def test_unwritable_mirror_root_is_reported(fake: FakeGitHub) -> None:
    root = runtime.mirror_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text("a file where the folder must be", encoding="utf-8")
    res = sync()
    assert res.status == "error" and res.message.startswith("Cannot write to")
    assert res.issues is None  # the job keeps the counts it has


# --- store ------------------------------------------------------------------

def test_store_read_api(fake: FakeGitHub) -> None:
    assert store.has_metadata(DEMO) is False
    assert store.load_index(DEMO) == [] and store.list_items(DEMO) == ([], 0)
    assert store.load_issue(DEMO, 1) is None and store.load_repo(DEMO) is None
    assert store.load_releases(DEMO) == [] and store.load_state(DEMO) == {}
    assert store.summary(DEMO)["issues"]["total"] == 0
    sync()
    assert store.has_metadata(DEMO) is True

    entry = next(e for e in store.load_index(DEMO) if e["number"] == 3)
    assert entry == {
        "number": 3, "kind": "pull", "title": "Fix crash", "state": "closed",
        "state_reason": None, "merged": True, "draft": False, "user": "cy", "labels": [],
        "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-07T10:00:00Z",
        "closed_at": "2024-01-07T10:00:00Z", "merged_at": "2024-01-07T10:00:00Z",
        "comments": 1, "review_comments": 2, "reviews": 1,
        "fetched_at": "2024-06-01T12:00:00Z",
    }
    first = next(e for e in store.load_index(DEMO) if e["number"] == 1)
    assert first["labels"] == [{"name": "bug", "color": "d73a4a"}] and first["merged"] is None

    items, total = store.list_items(DEMO)
    assert total == 5 and [i["number"] for i in items] == [5, 4, 3, 2, 1]
    assert [i["number"] for i in store.list_items(DEMO, kind="pull")[0]] == [5, 3]
    assert [i["number"] for i in store.list_items(DEMO, kind="issue", state="open")[0]] == [4, 2]
    assert [i["number"] for i in store.list_items(DEMO, state="closed")[0]] == [3, 1]
    assert [i["number"] for i in store.list_items(DEMO, state="merged")[0]] == [3]
    assert [i["number"] for i in store.list_items(DEMO, query="CRASH")[0]] == [3, 1]
    assert [i["number"] for i in store.list_items(DEMO, query="docs")[0]] == [2]  # label + title
    assert [i["number"] for i in store.list_items(DEMO, query="#4")[0]] == [4]
    assert [i["number"] for i in store.list_items(DEMO, query="cy")[0]] == [5, 3]  # author
    page2, total = store.list_items(DEMO, page=2, per_page=2)
    assert total == 5 and [i["number"] for i in page2] == [3, 2]
    assert store.list_items(DEMO, page=9, per_page=2) == ([], 5)

    assert store.summary(DEMO) == {
        "issues": {"open": 2, "closed": 1, "total": 3},
        "pulls": {"open": 1, "closed": 1, "merged": 1, "total": 2},
        "releases": 1,
    }
    with pytest.raises(ValueError):
        store.list_items(DEMO, kind="commit")
    with pytest.raises(ValueError):
        store.list_items(DEMO, state="draft")


@pytest.mark.parametrize("name", [
    "", "octo", "octo/", "/demo", "../demo", "octo/..", "octo/.", "octo/demo/x",
    "octo/../../etc", "octo\\demo", "octo/de mo", "C:/demo", "octo/demo\x00", ".octo/demo",
    "octo/demo\n", None, 5,
])
def test_store_refuses_unsafe_repository_names(fake: FakeGitHub, name: object) -> None:
    for call in (store.meta_dir, store.has_metadata, store.load_index, store.load_repo,
                 store.load_releases, store.summary, store.list_items):
        with pytest.raises(ValueError):
            call(name)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.load_issue(name, 1)  # type: ignore[arg-type]


@pytest.mark.parametrize("number", [0, -1, "1", "../1", 1.0, True, None])
def test_store_refuses_unsafe_numbers(fake: FakeGitHub, number: object) -> None:
    sync()
    with pytest.raises(ValueError):
        store.load_issue(DEMO, number)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.load_pull(DEMO, number)  # type: ignore[arg-type]


def test_store_paths_stay_inside_the_mirror_root(fake: FakeGitHub) -> None:
    root = runtime.mirror_root().resolve()
    for name in ("octo/demo", "a/b.c_d-e", "A-1/.github"):
        assert store.meta_dir(name).is_relative_to(root)
        assert store.item_path(name, "pull", 7).is_relative_to(root)
    with pytest.raises(ValueError):
        store.item_path(DEMO, "../x", 1)


# --- job, settings and routes -----------------------------------------------

def enrol(*names: str) -> None:
    for name in names:
        db.upsert_repo({"full_name": name, "private": 0, "fork": 0, "archived": 0,
                        "default_branch": "main", "mirror_path": f"{name}.git",
                        "discovered_at": time.time()})


def repo_row(name: str):
    return next(r for r in db.list_repos() if r["full_name"] == name)


@pytest.fixture
def app_fake(client: TestClient, fake: FakeGitHub) -> FakeGitHub:
    """The fake GitHub plus a started app (database, job registry)."""
    return fake


def test_job_captures_two_repositories(app_fake: FakeGitHub) -> None:
    second = app_fake.add_repo("octo/second")
    second.add_issue(1, "Only issue", "2024-03-01T00:00:00Z")
    enrol(DEMO, "octo/second", "octo/off", "octo/no-backup")
    db.toggle_repo_metadata(repo_row("octo/off")["id"])
    db.toggle_repo(repo_row("octo/no-backup")["id"])

    meta_job.run_metadata_job()
    run = db.recent_jobs(1)[0]
    assert (run["job_type"], run["status"], run["ok_count"], run["fail_count"]) == (
        "metadata", "ok", 2, 0)
    assert "2 ok" in run["summary"] and "6 items downloaded" in run["summary"]
    assert f"[ok] {DEMO}" in run["detail"] and "octo/off" not in run["detail"]
    demo = repo_row(DEMO)
    assert (demo["meta_status"], demo["meta_issues"], demo["meta_pulls"]) == ("ok", 3, 2)
    assert demo["meta_sync_at"] and "3 issues, 2 pull requests" in demo["meta_message"]
    assert repo_row("octo/second")["meta_issues"] == 1
    assert repo_row("octo/off")["meta_sync_at"] is None
    assert not store.has_metadata("octo/off") and not store.has_metadata("octo/no-backup")


def test_job_stops_on_rate_limit_and_continues_next_run(app_fake: FakeGitHub) -> None:
    for name in ("octo/second", "octo/third"):
        app_fake.add_repo(name).add_issue(1, "Only issue", "2024-03-01T00:00:00Z")
    enrol(DEMO, "octo/second", "octo/third")
    app_fake.budget = 21  # enough for octo/demo (19 requests), not for octo/second

    meta_job.run_metadata_job()
    run = db.recent_jobs(1)[0]
    assert run["status"] == "partial"
    assert "1 of 3 repositories done, 2 remain" in run["summary"]
    assert "resets at" in run["summary"] and "No GitHub token is set" in run["summary"]
    assert repo_row(DEMO)["meta_status"] == "ok"
    assert repo_row("octo/second")["meta_status"] == "rate_limited"
    assert repo_row("octo/third")["meta_sync_at"] is None
    assert "octo/third" not in "".join(app_fake.requests)

    app_fake.budget = None
    app_fake.requests.clear()
    meta_job.run_metadata_job()
    run = db.recent_jobs(1)[0]
    assert (run["status"], run["ok_count"]) == ("ok", 3)
    # The repository with no capture yet runs first, then the oldest capture.
    order = list(dict.fromkeys(p.split("/")[3] for p in app_fake.paths()))
    assert order == ["third", "demo", "second"]
    assert repo_row("octo/third")["meta_issues"] == 1


def test_job_records_a_failed_repository_and_continues(app_fake: FakeGitHub) -> None:
    enrol("octo/gone", DEMO)
    meta_job.run_metadata_job()
    run = db.recent_jobs(1)[0]
    assert (run["status"], run["ok_count"], run["fail_count"]) == ("partial", 1, 1)
    gone = repo_row("octo/gone")
    assert gone["meta_status"] == "error" and "does not show" in gone["meta_message"]
    assert repo_row(DEMO)["meta_status"] == "ok"


def test_job_is_skipped_when_the_setting_is_off(authed: tuple[TestClient, str],
                                                fake: FakeGitHub) -> None:
    client, token = authed
    assert db.get_setting("metadata_enabled") == "1"  # seeded default: on
    page = client.get("/settings").text
    assert "<h2>Metadata</h2>" in page and 'name="metadata_enabled" checked' in page
    assert 'name="schedule_metadata"' in page
    assert db.get_setting("schedule_metadata") == "0 4 * * *"

    client.post("/settings", data={"csrf_token": token})  # checkbox not sent = off
    assert db.get_bool("metadata_enabled", True) is False
    enrol(DEMO)
    meta_job.run_metadata_job()
    run = db.recent_jobs(1)[0]
    assert run["status"] == "skipped" and "off in Settings" in run["summary"]
    assert fake.requests == [] and not store.has_metadata(DEMO)

    client.post("/settings", data={"csrf_token": token, "metadata_enabled": "on"})
    assert db.get_bool("metadata_enabled") is True


def test_second_job_run_is_skipped_while_one_runs(app_fake: FakeGitHub) -> None:
    from app import jobs
    with jobs._LOCKS["metadata"]:
        meta_job.run_metadata_job()
    run = db.recent_jobs(1)[0]
    assert (run["job_type"], run["status"]) == ("metadata", "skipped")


def test_dashboard_and_repo_table_show_the_feature(authed: tuple[TestClient, str],
                                                   fake: FakeGitHub) -> None:
    client, _ = authed
    dash = client.get("/").text
    assert "Capture metadata" in dash and 'action="/run/metadata"' in dash
    enrol(DEMO)
    page = client.get("/repos").text
    assert "<th>Metadata</th>" in page and "not captured yet" in page
    meta_job.run_metadata_job()
    page = client.get("/repos").text
    assert "3 issues · 2 PRs" in page and "Last metadata sync:" in page


def test_repo_metadata_toggle_route(authed: tuple[TestClient, str]) -> None:
    client, token = authed
    enrol(DEMO)
    rid = repo_row(DEMO)["id"]
    url = f"/repos/{rid}/metadata-toggle"

    assert client.post(url).status_code == 403  # no CSRF token
    assert repo_row(DEMO)["metadata_enabled"] == 1

    resp = client.post(url, headers={"X-CSRF-Token": token, "HX-Request": "true"})
    assert resp.status_code == 200
    assert resp.text.lstrip().startswith(f'<td id="repo-meta-{rid}"')
    assert 'data-on="0"' in resp.text and 'aria-pressed="false"' in resp.text
    assert repo_row(DEMO)["metadata_enabled"] == 0
    assert repo_row(DEMO)["enabled"] == 1  # the backup switch is a different one

    resp = client.post(url, data={"csrf_token": token})  # plain form
    assert (resp.status_code, resp.headers["location"]) == (303, "/repos")
    assert repo_row(DEMO)["metadata_enabled"] == 1

    assert client.post("/repos/9999/metadata-toggle",
                       headers={"X-CSRF-Token": token}).status_code == 404


def test_repo_metadata_toggle_needs_a_session(client: TestClient) -> None:
    token_page = client.get("/login").text
    from .conftest import csrf_from
    resp = client.post("/repos/1/metadata-toggle",
                       headers={"X-CSRF-Token": csrf_from(token_page)})
    assert (resp.status_code, resp.headers["location"]) == (303, "/login")


def test_forbidden_answer_names_the_permission_to_change(fake: FakeGitHub) -> None:
    """A fine-grained token with no Issues permission gets HTTP 403 on the issue list."""
    fake.always[f"/repos/{DEMO}/issues"] = 403
    res = sync()
    assert res.status == "error"
    assert "HTTP 403" in res.message
    assert 'permission "Issues" to Read-only' in res.message
    assert "repository access" in res.message
