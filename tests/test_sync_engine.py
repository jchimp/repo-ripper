"""Sync engine: mirror step, pull/verify/scan jobs, live progress, and the UI.

Every "remote" is a local bare repository in `tmp_path`. No network.
"""
import os
import re
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, github, jobs, mirror, notify, progress, runtime, scheduler, viewhelpers
from app.routes import dashboard as dashboard_routes

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def git(*args: str, cwd: Path | None = None) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, env=_GIT_ENV, check=True,
                         capture_output=True, text=True)
    return res.stdout.strip()


class Remotes:
    """Local stand-ins for GitHub: `<root>/<owner>/<name>.git` bare repositories."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, full_name: str) -> Path:
        return self.root / f"{full_name}.git"

    def make(self, full_name: str) -> Path:
        work = self.root / "work" / full_name
        work.mkdir(parents=True)
        git("init", "-q", "-b", "main", cwd=work)
        self.commit(full_name, "first")
        bare = self.path(full_name)
        bare.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "-q", "--bare", str(work), str(bare))
        git("remote", "add", "origin", str(bare), cwd=work)
        return bare

    def commit(self, full_name: str, text: str, push: bool = False) -> str:
        work = self.root / "work" / full_name
        (work / "file.txt").write_text(f"{text}\n" * 50, encoding="utf-8")
        git("add", ".", cwd=work)
        git("commit", "-q", "-m", text, cwd=work)
        if push:
            git("push", "-q", "origin", "main", cwd=work)
        return git("rev-parse", "HEAD", cwd=work)


@pytest.fixture
def remotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Remotes:
    r = Remotes(tmp_path / "remotes")
    monkeypatch.setattr(mirror, "clone_url", lambda full_name: str(r.path(full_name)))
    monkeypatch.setattr(mirror, "RETRY_DELAY", 0)
    return r


@pytest.fixture
def app_db(authed: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
           ) -> tuple[TestClient, str]:
    """A signed-in client on a fresh database, with LFS, wiki and waits off."""
    db.set_setting("fetch_lfs", "0")
    db.set_setting("include_wikis", "0")
    monkeypatch.setattr(dashboard_routes, "START_WAIT", 0)
    return authed


def add_repo(full_name: str, **fields: object) -> int:
    """Insert one repository row and return its id."""
    db.upsert_repo({"full_name": full_name, "private": 0, "fork": 0, "archived": 0,
                    "default_branch": "main", "mirror_path": f"{full_name}.git",
                    "discovered_at": time.time()})
    with db.get_conn() as conn:
        for key, value in fields.items():
            conn.execute(f"UPDATE repos SET {key} = ? WHERE full_name = ?", (value, full_name))
        return conn.execute("SELECT id FROM repos WHERE full_name = ?",
                            (full_name,)).fetchone()[0]


def repo(full_name: str):
    return next(r for r in db.list_repos() if r["full_name"] == full_name)


def listing(*items: tuple[int, str]) -> list[dict]:
    return [{"id": gid, "full_name": name, "private": 0, "fork": 0, "archived": 0,
             "default_branch": "main", "has_wiki": False} for gid, name in items]


def run_scan(monkeypatch: pytest.MonkeyPatch, items: list[dict]) -> None:
    monkeypatch.setattr(github, "list_all_repos", lambda **_kw: items)
    # With no token the scan skips the "my repositories" part.
    monkeypatch.setattr(runtime, "github_token", lambda: "test-token")
    # The scan also checks the token with GitHub: no network in tests.
    monkeypatch.setattr(jobs.setup_state, "refresh_token_check", lambda: None)
    jobs.run_scan_job()


# --- mirror step ---------------------------------------------------------------

def test_clone_then_update(app_db, remotes: Remotes) -> None:
    remotes.make("octo/alpha")
    first = mirror.mirror_repo("octo/alpha", fetch_lfs=False, include_wiki=False, has_wiki=False)
    assert (first.ok, first.action, first.status) == (True, "clone", "ok")
    assert first.size_bytes and first.size_bytes > 0
    dest = mirror.mirror_dir("octo/alpha")
    assert (dest / "HEAD").is_file()
    assert not dest.with_name(dest.name + ".partial").exists()

    head = remotes.commit("octo/alpha", "second", push=True)
    second = mirror.mirror_repo("octo/alpha", fetch_lfs=False, include_wiki=False, has_wiki=False)
    assert (second.ok, second.action) == (True, "update")
    assert git("rev-parse", "refs/heads/main", cwd=dest) == head


def test_missing_remote_is_an_error_and_leaves_no_folder(app_db, remotes: Remotes) -> None:
    res = mirror.mirror_repo("octo/nothing", fetch_lfs=False, include_wiki=False, has_wiki=False)
    assert (res.ok, res.status, res.action) == (False, "error", "clone")
    assert "clone failed" in res.message
    parent = mirror.mirror_dir("octo/nothing").parent
    assert not parent.exists() or list(parent.iterdir()) == []


def test_partial_clone_does_not_block_the_next_clone(app_db, remotes: Remotes) -> None:
    remotes.make("octo/alpha")
    dest = mirror.mirror_dir("octo/alpha")
    # A folder with no HEAD (old half clone) and a .partial folder (killed run).
    (dest / "objects").mkdir(parents=True)
    (dest / "objects" / "junk").write_text("x")
    partial = dest.with_name(dest.name + ".partial")
    partial.mkdir()
    (partial / "HEAD").write_text("ref: refs/heads/main\n")

    res = mirror.mirror_repo("octo/alpha", fetch_lfs=False, include_wiki=False, has_wiki=False)
    assert (res.ok, res.action) == (True, "clone")
    assert not partial.exists()
    assert not (dest / "objects" / "junk").exists()
    assert git("rev-parse", "--is-bare-repository", cwd=dest) == "true"

    # The folder with no HEAD can be a damaged mirror: it is moved, not deleted.
    broken = [p for p in dest.parent.iterdir() if p.name.startswith("alpha.git.broken-")]
    assert len(broken) == 1
    assert re.fullmatch(r"alpha\.git\.broken-\d{8}-\d{6}", broken[0].name)
    assert (broken[0] / "objects" / "junk").read_text() == "x"
    assert res.status == "warn"
    assert broken[0].name in res.warnings[0] and "is kept" in res.warnings[0]


def test_folder_with_no_head_is_kept_when_the_clone_fails(app_db, remotes: Remotes) -> None:
    dest = mirror.mirror_dir("octo/nothing")                 # no remote: the clone fails
    (dest / "objects").mkdir(parents=True)
    (dest / "objects" / "junk").write_text("x")

    res = mirror.mirror_repo("octo/nothing", fetch_lfs=False, include_wiki=False, has_wiki=False)
    assert (res.ok, res.action) == (False, "clone")
    broken = [p for p in dest.parent.iterdir() if p.name.startswith("nothing.git.broken-")]
    assert len(broken) == 1 and (broken[0] / "objects" / "junk").is_file()
    assert broken[0].name in res.message                     # the user sees where it went


# --- commits that lose their name upstream ---------------------------------------

def _lost_log(dest: Path) -> list[list[str]]:
    path = dest / mirror.LOST_REFS_LOG
    if not path.exists():
        return []
    return [line.split(" ") for line in path.read_text(encoding="utf-8").splitlines()]


def _has_object(dest: Path, sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", sha], cwd=dest, env=_GIT_ENV,
                          capture_output=True).returncode == 0


def _has_reflog(dest: Path, ref: str) -> bool:
    return subprocess.run(["git", "reflog", "exists", ref], cwd=dest, env=_GIT_ENV,
                          capture_output=True).returncode == 0


def _age_objects(dest: Path, days: int = 60) -> None:
    """Make all object files old, so that the age limit of `git gc` does not keep them."""
    old = time.time() - days * 86400
    for path in (dest / "objects").rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))


def _sync_alpha() -> mirror.MirrorResult:
    return mirror.mirror_repo("octo/alpha", fetch_lfs=False, include_wiki=False, has_wiki=False)


def test_mirror_is_set_to_keep_unreachable_objects(app_db, remotes: Remotes) -> None:
    remotes.make("octo/alpha")
    assert _sync_alpha().status == "ok"
    dest = mirror.mirror_dir("octo/alpha")
    wanted = {"gc.pruneexpire": "never", "core.logallrefupdates": "always",
              "gc.reflogexpire": "never", "gc.reflogexpireunreachable": "never"}
    for key, value in wanted.items():
        assert git("config", "--local", "--get", key, cwd=dest) == value

    # A mirror of an older version gets the values at its next update, one time.
    for key in wanted:
        git("config", "--local", "--unset", key, cwd=dest)
    assert _sync_alpha().status == "ok"
    for key, value in wanted.items():
        assert git("config", "--local", "--get-all", key, cwd=dest) == value
    assert _sync_alpha().status == "ok"
    for key, value in wanted.items():
        assert git("config", "--local", "--get-all", key, cwd=dest) == value


def test_deleted_branch_keeps_its_commits(app_db, remotes: Remotes) -> None:
    remotes.make("octo/alpha")
    work = remotes.root / "work" / "octo/alpha"
    git("checkout", "-q", "-b", "topic", cwd=work)
    remotes.commit("octo/alpha", "topic work")
    git("push", "-q", "origin", "topic", cwd=work)
    assert _sync_alpha().ok
    topic = remotes.commit("octo/alpha", "more topic work")
    git("push", "-q", "origin", "topic", cwd=work)
    git("checkout", "-q", "main", cwd=work)
    assert _sync_alpha().message == "update"
    dest = mirror.mirror_dir("octo/alpha")
    assert git("rev-parse", "refs/heads/topic", cwd=dest) == topic
    assert _has_reflog(dest, "refs/heads/topic")             # written by the update

    git("push", "-q", "origin", "--delete", "topic", cwd=work)
    res = _sync_alpha()
    assert (res.ok, res.action, res.status) == (True, "update", "ok")
    assert res.message == "update, 1 old ref kept"
    # The mirror follows the deletion of the name ...
    assert git("for-each-ref", "refs/heads/topic", cwd=dest) == ""
    # ... and git removes the reflog of a deleted branch, so the log file is
    # the only record of the id.
    assert not _has_reflog(dest, "refs/heads/topic")
    lines = _lost_log(dest)
    assert len(lines) == 1
    stamp, kind, ref, old = lines[0]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp)
    assert (kind, ref, old) == ("deleted", "refs/heads/topic", topic)

    # The commit is in the mirror, also after a garbage collection of old objects.
    assert _has_object(dest, topic)
    _age_objects(dest)
    git("gc", "-q", cwd=dest)
    assert _has_object(dest, topic)
    assert git("fsck", "--no-dangling", cwd=dest) == ""

    # The next update adds no line and does not lose the commit.
    remotes.commit("octo/alpha", "second", push=True)
    assert _sync_alpha().message == "update"
    assert len(_lost_log(dest)) == 1 and _has_object(dest, topic)

    # Control: with the default age limit of git, the same collection removes
    # the commit. So `gc.pruneExpire=never` is what keeps it.
    _age_objects(dest)
    git("-c", "gc.pruneExpire=2.weeks.ago", "gc", "-q", cwd=dest)
    assert not _has_object(dest, topic)


def test_force_push_keeps_the_old_commits(app_db, remotes: Remotes) -> None:
    remotes.make("octo/alpha")
    work = remotes.root / "work" / "octo/alpha"
    assert _sync_alpha().ok
    dest = mirror.mirror_dir("octo/alpha")
    # A clone writes no reflog. The first update of a branch starts it.
    assert not _has_reflog(dest, "refs/heads/main")
    old = remotes.commit("octo/alpha", "second", push=True)
    assert _sync_alpha().message == "update"

    git("reset", "-q", "--hard", "HEAD~1", cwd=work)
    new = remotes.commit("octo/alpha", "a different second")
    git("push", "-q", "--force", "origin", "main", cwd=work)
    res = _sync_alpha()
    assert (res.status, res.message) == ("ok", "update, 1 old ref kept")
    assert git("rev-parse", "refs/heads/main", cwd=dest) == new
    lines = _lost_log(dest)
    assert len(lines) == 1
    assert lines[0][1:] == ["forced", "refs/heads/main", old, new]

    # The update of a bare mirror writes the reflog, so the old tip has a name.
    assert old in git("reflog", "show", "--format=%H", "refs/heads/main", cwd=dest).splitlines()
    assert _has_object(dest, old)
    _age_objects(dest)
    git("gc", "-q", cwd=dest)
    assert _has_object(dest, old)


def test_moved_tag_is_recorded(app_db, remotes: Remotes) -> None:
    remotes.make("octo/alpha")
    work = remotes.root / "work" / "octo/alpha"
    git("tag", "-a", "-m", "one", "v1", cwd=work)
    git("push", "-q", "origin", "v1", cwd=work)
    old_tag = git("rev-parse", "refs/tags/v1", cwd=work)
    assert _sync_alpha().ok
    dest = mirror.mirror_dir("octo/alpha")

    # The new tag points to a later commit: for commits that is a fast-forward,
    # but the old tag object has no name after the update.
    remotes.commit("octo/alpha", "second", push=True)
    git("tag", "-f", "-a", "-m", "one again", "v1", cwd=work)
    git("push", "-q", "--force", "origin", "v1", cwd=work)
    assert _sync_alpha().message == "update, 1 old ref kept"
    assert _lost_log(dest)[0][1:4] == ["forced", "refs/tags/v1", old_tag]
    assert _has_object(dest, old_tag)


def test_fast_forward_writes_no_line(app_db, remotes: Remotes) -> None:
    remotes.make("octo/alpha")
    work = remotes.root / "work" / "octo/alpha"
    assert _sync_alpha().ok
    remotes.commit("octo/alpha", "second", push=True)
    git("checkout", "-q", "-b", "new-branch", cwd=work)
    git("push", "-q", "origin", "new-branch", cwd=work)
    res = _sync_alpha()
    assert (res.status, res.message) == ("ok", "update")
    assert not (mirror.mirror_dir("octo/alpha") / mirror.LOST_REFS_LOG).exists()


def test_generated_merge_refs_of_pull_requests_are_not_recorded(app_db,
                                                                remotes: Remotes) -> None:
    remote = remotes.make("octo/alpha")
    first = git("rev-parse", "refs/heads/main", cwd=remote)
    git("update-ref", "refs/pull/7/merge", first, cwd=remote)
    git("update-ref", "refs/pull/7/head", first, cwd=remote)
    assert _sync_alpha().ok
    git("update-ref", "-d", "refs/pull/7/merge", cwd=remote)
    git("update-ref", "-d", "refs/pull/7/head", cwd=remote)
    assert _sync_alpha().message == "update, 1 old ref kept"
    assert [line[2] for line in _lost_log(mirror.mirror_dir("octo/alpha"))] == [
        "refs/pull/7/head"]


def test_prune_lets_a_branch_name_become_a_folder(app_db, remotes: Remotes) -> None:
    """Why the update keeps `--prune`: branch `a` deleted, branch `a/b` made."""
    remotes.make("octo/alpha")
    work = remotes.root / "work" / "octo/alpha"
    git("push", "-q", "origin", "main:refs/heads/a", cwd=work)
    assert _sync_alpha().ok
    git("push", "-q", "origin", "--delete", "a", cwd=work)
    git("push", "-q", "origin", "main:refs/heads/a/b", cwd=work)
    res = _sync_alpha()
    assert res.ok, res.message
    dest = mirror.mirror_dir("octo/alpha")
    assert git("for-each-ref", "--format=%(refname)", "refs/heads/a", cwd=dest) == (
        "refs/heads/a/b")
    assert [line[1:3] for line in _lost_log(dest)] == [["deleted", "refs/heads/a"]]


def test_lost_refs_log_that_cannot_be_written_is_a_warning(app_db, remotes: Remotes) -> None:
    remotes.make("octo/alpha")
    work = remotes.root / "work" / "octo/alpha"
    git("push", "-q", "origin", "main:refs/heads/topic", cwd=work)
    assert _sync_alpha().ok
    dest = mirror.mirror_dir("octo/alpha")
    topic = git("rev-parse", "refs/heads/topic", cwd=dest)
    (dest / mirror.LOST_REFS_LOG).mkdir()                    # a folder: the open fails
    git("push", "-q", "origin", "--delete", "topic", cwd=work)

    res = _sync_alpha()
    assert (res.ok, res.status, res.message) == (True, "warn", "update")
    assert mirror.LOST_REFS_LOG in res.warnings[0]
    assert f"deleted refs/heads/topic {topic}" in res.warnings[0]     # the id is not lost


def test_recover_a_deleted_branch_with_the_readme_commands(app_db, remotes: Remotes,
                                                           tmp_path: Path) -> None:
    remotes.make("octo/alpha")
    work = remotes.root / "work" / "octo/alpha"
    git("checkout", "-q", "-b", "topic", cwd=work)
    remotes.commit("octo/alpha", "topic work")
    git("push", "-q", "origin", "topic", cwd=work)
    assert _sync_alpha().ok
    git("push", "-q", "origin", "--delete", "topic", cwd=work)
    assert _sync_alpha().ok
    dest = mirror.mirror_dir("octo/alpha")
    old = _lost_log(dest)[0][3]

    # Way 1: do not change the mirror. Clone, then fetch the id.
    restored = tmp_path / "restored"
    git("clone", "-q", str(dest), str(restored))
    git("fetch", "-q", "origin", old, cwd=restored)
    git("branch", "recovered", old, cwd=restored)
    assert git("rev-parse", "recovered", cwd=restored) == old
    assert git("log", "-1", "--format=%s", "recovered", cwd=restored) == "topic work"

    # Way 2: give the commit a branch name in the mirror, then clone.
    git("--git-dir", str(dest), "branch", "recovered", old)
    second = tmp_path / "restored2"
    git("clone", "-q", str(dest), str(second))
    assert git("rev-parse", "origin/recovered", cwd=second) == old
    # The next update removes that name again (it is not on GitHub) and
    # records it. The commit stays.
    assert _sync_alpha().message == "update, 1 old ref kept"
    assert git("for-each-ref", "refs/heads/recovered", cwd=dest) == ""
    assert _has_object(dest, old)


# --- wiki ------------------------------------------------------------------------

def _sync_alpha_with_wiki() -> mirror.MirrorResult:
    return mirror.mirror_repo("octo/alpha", fetch_lfs=False, include_wiki=True, has_wiki=True)


def test_wiki_that_does_not_exist_is_not_a_warning(app_db, remotes: Remotes) -> None:
    remotes.make("octo/alpha")
    res = _sync_alpha_with_wiki()
    assert (res.status, res.message, res.warnings) == ("ok", "clone, wiki absent", [])
    res = _sync_alpha_with_wiki()                            # the same on each later run
    assert (res.status, res.message, res.warnings) == ("ok", "update, wiki absent", [])


def test_failed_update_of_a_wiki_on_disk_is_a_warning(app_db, remotes: Remotes) -> None:
    remotes.make("octo/alpha")
    wiki_remote = remotes.make("octo/alpha.wiki")
    res = _sync_alpha_with_wiki()
    assert (res.status, res.message) == ("ok", "clone, wiki ok")
    assert (mirror.mirror_dir("octo/alpha.wiki") / "HEAD").is_file()

    wiki_remote.rename(wiki_remote.with_name("moved-away.git"))
    res = _sync_alpha_with_wiki()
    assert (res.ok, res.status, res.message) == (True, "warn", "update, wiki failed")
    assert len(res.warnings) == 1 and res.warnings[0].startswith("wiki update failed: ")
    assert (mirror.mirror_dir("octo/alpha.wiki") / "HEAD").is_file()     # the copy stays


def test_wiki_mirror_keeps_old_commits_too(app_db, remotes: Remotes) -> None:
    remotes.make("octo/alpha")
    remotes.make("octo/alpha.wiki")
    work = remotes.root / "work" / "octo/alpha.wiki"
    old = remotes.commit("octo/alpha.wiki", "page two", push=True)
    assert _sync_alpha_with_wiki().ok
    git("reset", "-q", "--hard", "HEAD~1", cwd=work)
    remotes.commit("octo/alpha.wiki", "page two, again")
    git("push", "-q", "--force", "origin", "main", cwd=work)

    res = _sync_alpha_with_wiki()
    assert (res.status, res.message) == ("ok", "update, wiki ok, wiki: 1 old ref kept")
    wiki = mirror.mirror_dir("octo/alpha.wiki")
    assert _lost_log(wiki)[0][1:4] == ["forced", "refs/heads/main", old]
    assert _has_object(wiki, old)


def test_lfs_failure_is_a_warning_with_the_reason(app_db, remotes: Remotes,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    remotes.make("octo/alpha")
    real_run = mirror._run

    def fake_run(cmd: list[str], timeout: int = 0, **kw: bool) -> tuple[int, str]:
        if "lfs" in cmd:
            return 2, "batch response: Rate limit exceeded"
        return real_run(cmd, **kw)

    monkeypatch.setattr(mirror, "_run", fake_run)
    res = mirror.mirror_repo("octo/alpha", fetch_lfs=True, include_wiki=False, has_wiki=False)
    assert (res.ok, res.status) == (True, "warn")
    assert "Rate limit exceeded" in res.warnings[0]


def test_network_error_is_retried_one_time(app_db, remotes: Remotes,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    remotes.make("octo/alpha")
    real_run = mirror._run
    calls: list[list[str]] = []

    def flaky(cmd: list[str], timeout: int = 0, **kw: bool) -> tuple[int, str]:
        if "clone" in cmd:
            calls.append(cmd)
            if len(calls) == 1:
                return 128, "fatal: unable to access: Could not resolve host: github.com"
        return real_run(cmd, **kw)

    monkeypatch.setattr(mirror, "_run", flaky)
    res = mirror.mirror_repo("octo/alpha", fetch_lfs=False, include_wiki=False, has_wiki=False)
    assert res.ok and len(calls) == 2
    assert "retry" in res.message

    # A failure that is not a network error is not retried.
    calls.clear()
    monkeypatch.setattr(mirror, "_run", lambda cmd, timeout=0, **kw: (
        calls.append(cmd) or (128, "fatal: repository not found")) if "clone" in cmd
        else real_run(cmd, **kw))
    res = mirror.mirror_repo("octo/beta", fetch_lfs=False, include_wiki=False, has_wiki=False)
    assert not res.ok and len(calls) == 1


def test_dir_size_counts_file_bytes(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "one.bin").write_bytes(b"x" * 1000)
    (tmp_path / "a" / "two.bin").write_bytes(b"x" * 234)
    (tmp_path / "a" / "b" / "three.bin").write_bytes(b"")
    assert mirror.dir_size(tmp_path) == 1234
    assert mirror.dir_size(tmp_path / "missing") is None


def test_token_is_scrubbed_from_stored_text(app_db, monkeypatch: pytest.MonkeyPatch) -> None:
    token = "ghp_SECRETsecretSECRETsecret1234"
    monkeypatch.setattr(mirror.runtime, "github_token", lambda: token)
    header = mirror._auth_header(token)
    encoded = mirror._basic_credential(token)
    assert header == f"Authorization: Basic {encoded}"
    text = mirror.scrub(f"fatal: bad config {header}\nurl https://user:{token}@github.com/x\n"
                        f"plain {token} and {encoded}")
    assert token not in text
    assert encoded not in text
    assert "Authorization: Basic ***" in text
    assert "https://***@github.com/x" in text

    # The text that _run returns is scrubbed too.
    code, out = mirror._run(["git", "-c", f"http.extraHeader={header}",
                             "not-a-git-command", token])
    assert code != 0
    assert token not in out and encoded not in out


# --- pull job --------------------------------------------------------------------

def test_pull_job_goes_on_after_a_failure(app_db, remotes: Remotes,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("octo/alpha", "octo/beta", "octo/gamma"):
        remotes.make(name)
        add_repo(name)
    add_repo("octo/broken")                        # no remote: the clone fails
    add_repo("octo/off", enabled=0)
    add_repo("octo/gone", upstream_state="gone")
    sent: list[tuple[str, bool, str]] = []
    monkeypatch.setattr(notify, "notify_job", lambda *a: sent.append(a))
    db.set_setting("pull_workers", "3")

    jobs.run_pull_job()

    job = db.recent_jobs(1)[0]
    assert (job["job_type"], job["status"]) == ("pull", "partial")
    assert (job["ok_count"], job["fail_count"]) == (3, 1)
    assert "gone upstream" in job["summary"]
    # One log entry per repository (the text of an error can have more lines).
    assert sum(line.startswith("[") for line in job["detail"].splitlines()) == 4
    assert "[error] octo/broken" in job["detail"]
    for name in ("octo/alpha", "octo/beta", "octo/gamma"):
        assert repo(name)["last_status"] == "ok"
        assert repo(name)["last_ok_at"]
        assert repo(name)["size_bytes"] > 0
    assert repo("octo/broken")["last_status"] == "error"
    assert repo("octo/broken")["last_ok_at"] is None
    assert repo("octo/off")["last_status"] is None
    assert repo("octo/gone")["last_status"] is None

    # One log row per repository that was in the job.
    results = db.job_repo_results(job["id"])
    assert len(results) == 4
    assert results[0]["full_name"] == "octo/broken"          # failed first
    assert {r["action"] for r in results} == {"clone"}
    assert all(r["duration"] >= 0 for r in results)

    # History snapshot and the notification text.
    history = db.history_snapshots()
    assert len(history) == 1
    assert (history[0]["ok_count"], history[0]["fail_count"]) == (3, 1)
    assert history[0]["repo_count"] == 6
    assert history[0]["total_bytes"] == sum(r["size_bytes"] or 0 for r in db.list_repos())
    assert sent[0][0] == "pull" and sent[0][1] is False
    assert "Failed: octo/broken" in sent[0][2]

    # The registry is empty and nothing stays reserved.
    assert progress.active() == []
    assert jobs._ACTIVE == set()


def test_failed_names_text_is_limited() -> None:
    assert jobs.failed_names_text([]) == ""
    assert jobs.failed_names_text(["a/b"]) == "\nFailed: a/b"
    names = [f"o/r{i:02d}" for i in range(13)]
    text = jobs.failed_names_text(names)
    assert "o/r09" in text and "o/r10" not in text
    assert text.endswith("+3 more")


def test_pull_job_with_a_warning(app_db, remotes: Remotes,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    remotes.make("octo/alpha")
    add_repo("octo/alpha")
    db.set_setting("fetch_lfs", "1")
    real_run = mirror._run
    monkeypatch.setattr(mirror, "_run", lambda cmd, timeout=0, **kw: (
        (1, "lfs: smudge filter failed") if "lfs" in cmd else real_run(cmd, **kw)))
    jobs.run_pull_job()
    job = db.recent_jobs(1)[0]
    assert (job["status"], job["ok_count"], job["fail_count"]) == ("warn", 1, 0)
    row = repo("octo/alpha")
    assert row["last_status"] == "warn"
    assert "smudge filter failed" in row["last_error"]
    assert "smudge filter failed" in db.repo_sync_log(row["id"])[0]["message"]


def test_sync_log_keeps_the_newest_rows(app_db, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_id = add_repo("octo/alpha")
    monkeypatch.setattr(db, "SYNC_LOG_KEEP", 3)
    for i in range(6):
        db.add_sync_log(repo_id, None, 100.0 + i, 101.0 + i, "update", "ok", f"run {i}", 10)
    rows = db.repo_sync_log(repo_id)
    assert [r["message"] for r in rows] == ["run 5", "run 4", "run 3"]


def test_job_that_raises_is_closed(app_db, monkeypatch: pytest.MonkeyPatch) -> None:
    add_repo("octo/alpha")

    def boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(jobs, "_sync_repos", boom)
    jobs.run_pull_job()
    job = db.recent_jobs(1)[0]
    assert job["status"] == "error" and "disk on fire" in job["summary"]
    assert not jobs.is_running("pull")


# --- progress registry -----------------------------------------------------------

def test_progress_registry() -> None:
    jid = 987654
    assert progress.snapshot(jid) is None
    progress.start(jid, "pull", "Pull mirrors", 4)
    progress.begin_item(jid, "o/a")
    progress.begin_item(jid, "o/b")
    assert progress.running_names() >= {"o/a", "o/b"}
    progress.end_item(jid, "o/a", "ok", "[ok] o/a")
    progress.end_item(jid, "o/b", "error", "[error] o/b")
    progress.begin_item(jid, "o/c")
    progress.end_item(jid, "o/c", "warn", "[warn] o/c")
    snap = progress.snapshot(jid)
    assert (snap["done"], snap["total"], snap["percent"]) == (3, 4, 75)
    assert (snap["ok"], snap["warn"], snap["failed"]) == (1, 1, 1)
    assert snap["running"] == []
    assert snap["lines"] == ["[ok] o/a", "[error] o/b", "[warn] o/c"]
    assert progress.log_text(jid).count("\n") == 2
    snap["lines"].append("not shared")                 # a snapshot is a copy
    assert len(progress.snapshot(jid)["lines"]) == 3
    assert any(s["job_id"] == jid for s in progress.active())
    progress.finish(jid)
    assert progress.snapshot(jid) is None
    progress.end_item(jid, "o/x", "ok", "late")        # after finish: no error


# --- stale jobs ------------------------------------------------------------------

def test_running_jobs_are_closed_at_startup(app_db) -> None:
    client, _ = app_db
    stale = db.start_job("pull")
    done = db.start_job("scan")
    db.finish_job(done, "ok", 1, 0, "fine", "")
    assert db.mark_interrupted_jobs() == 1
    job = db.get_job(stale)
    assert job["status"] == "interrupted"
    assert job["finished_at"] is not None
    assert job["summary"] == "Interrupted by a restart"
    assert db.get_job(done)["status"] == "ok"
    assert 'class="pill interrupted"' in client.get("/jobs").text
    assert "hx-trigger" not in client.get(f"/jobs/{stale}").text


def test_startup_calls_the_stale_job_cleanup(app_db, tmp_path: Path) -> None:
    from app.main import app
    stale = db.start_job("pull")
    with TestClient(app):                              # a second start on the same database
        assert db.get_job(stale)["status"] == "interrupted"


# --- scan: renamed and gone ------------------------------------------------------

def test_scan_stores_the_github_id(app_db, monkeypatch: pytest.MonkeyPatch) -> None:
    run_scan(monkeypatch, listing((11, "octo/alpha"), (12, "octo/beta")))
    assert repo("octo/alpha")["github_id"] == 11
    assert repo("octo/beta")["github_id"] == 12
    assert repo("octo/alpha")["upstream_state"] == "ok"


def test_rename_by_github_id_moves_the_folders(app_db, remotes: Remotes,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    remotes.make("octo/old-name")
    run_scan(monkeypatch, listing((11, "octo/old-name"), (12, "octo/other")))
    old_id = repo("octo/old-name")["id"]
    assert mirror.mirror_repo("octo/old-name", fetch_lfs=False, include_wiki=False,
                              has_wiki=False).ok
    db.record_repo_result("octo/old-name", "ok", None, 123)
    root = mirror.mirror_dir("octo/old-name").parent
    (root / "old-name.wiki.git").mkdir()
    (root / "old-name.meta").mkdir()

    run_scan(monkeypatch, listing((11, "new-owner/new-name"), (12, "octo/other")))

    names = {r["full_name"] for r in db.list_repos()}
    assert names == {"new-owner/new-name", "octo/other"}
    row = repo("new-owner/new-name")
    assert row["id"] == old_id                       # the same row: history is kept
    assert row["mirror_path"] == "new-owner/new-name.git"
    assert row["upstream_state"] == "ok"
    assert row["last_status"] == "ok"
    new_root = mirror.mirror_dir("new-owner/new-name").parent
    assert (new_root / "new-name.git" / "HEAD").is_file()
    assert (new_root / "new-name.wiki.git").is_dir()
    assert (new_root / "new-name.meta").is_dir()
    assert not (root / "old-name.git").exists()
    job = db.recent_jobs(1)[0]
    assert "1 renamed" in job["summary"]
    assert "octo/old-name is now new-owner/new-name" in job["detail"]


def test_rename_leaves_the_files_when_the_target_exists(app_db,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    run_scan(monkeypatch, listing((11, "octo/old-name")))
    old = mirror.mirror_dir("octo/old-name")
    new = mirror.mirror_dir("octo/new-name")
    old.mkdir(parents=True)
    (old / "HEAD").write_text("old")
    new.mkdir(parents=True)
    (new / "HEAD").write_text("in the way")

    run_scan(monkeypatch, listing((11, "octo/new-name")))

    assert repo("octo/new-name")["github_id"] == 11
    assert (old / "HEAD").read_text() == "old"       # not moved, not deleted
    assert (new / "HEAD").read_text() == "in the way"
    detail = db.recent_jobs(1)[0]["detail"]
    assert "Not moved" in detail and "already exists" in detail


def test_gone_upstream_is_marked_and_cleared(app_db, remotes: Remotes,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = app_db
    remotes.make("octo/alpha")
    run_scan(monkeypatch, listing((11, "octo/alpha"), (12, "octo/beta")))
    add_repo("friend/watched", source="watched")
    jobs.run_pull_job()
    assert (mirror.mirror_dir("octo/alpha") / "HEAD").is_file()

    run_scan(monkeypatch, listing((12, "octo/beta")))
    gone = repo("octo/alpha")
    assert gone["upstream_state"] == "gone"
    assert gone["gone_since"] is not None
    assert repo("octo/beta")["upstream_state"] == "ok"
    assert repo("friend/watched")["upstream_state"] == "ok"       # not judged here
    assert "1 gone upstream" in db.recent_jobs(1)[0]["summary"]
    # The mirror is kept, and the pull job leaves the repository alone.
    synced_at = gone["last_sync_at"]
    jobs.run_pull_job()
    assert (mirror.mirror_dir("octo/alpha") / "HEAD").is_file()
    assert repo("octo/alpha")["last_sync_at"] == synced_at
    assert "gone upstream" in client.get("/repos").text
    assert "<b>1</b> gone upstream" in client.get("/").text

    # The time first seen gone does not move on the next scan.
    run_scan(monkeypatch, listing((12, "octo/beta")))
    assert repo("octo/alpha")["gone_since"] == gone["gone_since"]

    run_scan(monkeypatch, listing((11, "octo/alpha"), (12, "octo/beta")))
    back = repo("octo/alpha")
    assert (back["upstream_state"], back["gone_since"]) == ("ok", None)


def test_empty_listing_marks_nothing_gone(app_db, monkeypatch: pytest.MonkeyPatch) -> None:
    run_scan(monkeypatch, listing((11, "octo/alpha"), (12, "octo/beta")))
    run_scan(monkeypatch, [])
    assert {r["upstream_state"] for r in db.list_repos()} == {"ok"}
    summary = db.recent_jobs(1)[0]["summary"]
    assert "0 repositories" in summary and "No repository was marked as gone" in summary


def test_filtered_out_forks_are_not_gone(app_db, monkeypatch: pytest.MonkeyPatch) -> None:
    run_scan(monkeypatch, listing((11, "octo/alpha"), (12, "octo/a-fork")))
    with db.get_conn() as conn:
        conn.execute("UPDATE repos SET fork = 1 WHERE full_name = 'octo/a-fork'")
    db.set_setting("include_forks", "0")
    run_scan(monkeypatch, listing((11, "octo/alpha")))     # the listing now has no forks
    assert repo("octo/a-fork")["upstream_state"] == "ok"


# --- verify job ------------------------------------------------------------------

def _corrupt_one_object(mirror_path: Path) -> None:
    objects = [p for p in (mirror_path / "objects").rglob("*")
               if p.is_file() and p.parent.name not in ("info", "pack")
               or p.suffix == ".pack"]
    assert objects, "the mirror has no object file"
    target = objects[0]
    # A local clone can hard-link the object files. Replace the file, so that
    # the "remote" keeps its good copy.
    target.chmod(0o644)
    target.unlink()
    target.write_bytes(b"this is not a git object")


def test_verify_job_ok_and_corrupt(app_db, remotes: Remotes,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = app_db
    for name in ("octo/alpha", "octo/beta"):
        remotes.make(name)
        add_repo(name)
    add_repo("octo/never-synced")
    jobs.run_pull_job()
    sent: list[tuple[str, bool, str]] = []
    monkeypatch.setattr(notify, "notify_job", lambda *a: sent.append(a))

    jobs.run_verify_job()
    job = db.recent_jobs(1)[0]
    assert (job["job_type"], job["status"]) == ("verify", "ok")
    assert job["summary"] == "Verified 2 mirrors: 2 ok, 0 failed"
    assert repo("octo/alpha")["last_verify_status"] == "ok"
    assert repo("octo/alpha")["last_verify_at"]
    assert repo("octo/never-synced")["last_verify_at"] is None

    _corrupt_one_object(mirror.mirror_dir("octo/beta"))
    jobs.run_verify_job()
    job = db.recent_jobs(1)[0]
    assert (job["status"], job["ok_count"], job["fail_count"]) == ("partial", 1, 1)
    bad = repo("octo/beta")
    assert bad["last_verify_status"] == "error"
    assert "fsck failed" in bad["last_verify_message"]
    assert bad["last_status"] == "ok"               # verify does not change the sync status
    assert repo("octo/alpha")["last_verify_status"] == "ok"
    assert sent[-1][1] is False and "Failed: octo/beta" in sent[-1][2]
    assert db.job_repo_results(job["id"])[0]["action"] == "verify"
    assert "verify failed" in client.get("/repos").text
    assert "<b>1</b> verify failed" in client.get("/").text
    assert "Verification failed." in client.get(f"/repos/{bad['id']}").text
    assert "verify" in jobs.JOBS and jobs.JOBS["verify"].default_cron == "0 6 * * 6"


# --- chart -----------------------------------------------------------------------

def test_size_chart_empty_states() -> None:
    assert viewhelpers.size_chart([]) is None
    assert viewhelpers.size_chart([(1_700_000_000.0, 5000)]) is None


def test_size_chart_with_points() -> None:
    day = 86400
    points = [(1_700_000_000.0 + i * day, (i + 1) * 300 * 1024 * 1024) for i in range(8)]
    svg = str(viewhelpers.size_chart(points))
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert 'role="img"' in svg and "aria-label=" in svg and "<title>" in svg
    assert svg.count('class="chart-dot"') == 8
    assert 'class="chart-line"' in svg and 'class="chart-area"' in svg
    assert "GB" in svg                              # axis labels in a readable unit
    assert svg.count('class="chart-label"') >= 4
    # Colours come from the style sheet, never from the SVG.
    for forbidden in ("#", "rgb(", "fill=", "stroke=", "style="):
        assert forbidden not in svg, forbidden
    # Two points with the same time and size 0 must not divide by zero.
    assert viewhelpers.size_chart([(5.0, 0), (5.0, 0)]) is not None
    many = [(1_700_000_000.0 + i * day, 1000 + i) for i in range(80)]
    assert str(viewhelpers.size_chart(many)).count('class="chart-dot"') == 1


def test_relative_time_and_duration() -> None:
    now = 1_700_000_000.0
    assert viewhelpers.rel_time(None) == "never"
    assert viewhelpers.rel_time(now - 10, now) == "just now"
    assert viewhelpers.rel_time(now - 300, now) == "5 min ago"
    assert viewhelpers.rel_time(now - 3 * 3600, now) == "3 h ago"
    assert viewhelpers.rel_time(now - 4 * 86400, now) == "4 d ago"
    html = str(viewhelpers.ago(now))
    assert html.startswith("<time datetime=") and 'title="' in html
    assert viewhelpers.fmt_duration(8.2) == "8 s"
    assert viewhelpers.fmt_duration(125) == "2 min 5 s"
    assert viewhelpers.fmt_duration(3790) == "1 h 3 min"


# --- repositories page -----------------------------------------------------------

def _names(html: str) -> list[str]:
    import re
    return re.findall(r'class="repo-name" href="/repos/\d+">([^<]+)<', html)


@pytest.fixture
def inventory(app_db) -> dict[str, int]:
    now = time.time()
    return {
        "octo/alpha": add_repo("octo/alpha", last_status="ok", last_sync_at=now - 60,
                               size_bytes=3000),
        "octo/beta": add_repo("octo/beta", last_status="error", last_sync_at=now - 7200,
                              size_bytes=9000, private=1, last_error="clone failed: no"),
        "octo/gamma_x": add_repo("octo/gamma_x", last_status="warn", last_sync_at=now - 30,
                                 size_bytes=100, enabled=0),
        "octo/delta": add_repo("octo/delta", upstream_state="gone"),
        "friend/tool": add_repo("friend/tool", source="watched", last_status="ok",
                                last_sync_at=now - 500, size_bytes=50,
                                last_verify_status="error"),
    }


def test_repo_list_filters_search_and_sort(app_db, inventory: dict[str, int]) -> None:
    client, _ = app_db

    def names(query: str) -> list[str]:
        resp = client.get(f"/repos{query}")
        assert resp.status_code == 200
        return _names(resp.text)

    assert names("") == ["friend/tool", "octo/alpha", "octo/beta", "octo/delta", "octo/gamma_x"]
    assert names("?q=ALPH") == ["octo/alpha"]
    assert names("?q=_") == ["octo/gamma_x"]            # "_" is literal, not a wildcard
    assert names("?q=%25") == []
    assert names("?status=error") == ["octo/beta"]
    assert names("?status=warn") == ["octo/gamma_x"]
    assert names("?status=pending") == ["octo/delta"]
    assert names("?status=running") == []
    assert names("?visibility=private") == ["octo/beta"]
    assert names("?source=watched") == ["friend/tool"]
    assert names("?backup=off") == ["octo/gamma_x"]
    assert names("?flag=gone") == ["octo/delta"]
    assert names("?flag=verify") == ["friend/tool"]
    assert names("?status=ok&source=mine") == ["octo/alpha"]
    assert names("?sort=size")[:2] == ["octo/beta", "octo/alpha"]
    assert names("?sort=synced")[:2] == ["octo/gamma_x", "octo/alpha"]
    assert names("?sort=status")[:2] == ["octo/beta", "octo/gamma_x"]
    assert names("?sort=name_desc")[0] == "octo/gamma_x"
    assert names("?sort=;DROP TABLE repos")[0] == "friend/tool"    # unknown sort: default

    page = client.get("/repos?status=error").text
    assert "1 shown of 5" in page
    assert '<option value="error" selected>' in page
    assert 'hx-push-url="true"' in page
    assert 'hx-trigger="input changed delay:250ms, change"' in page
    assert "every 2s" not in page                       # idle: no polling
    assert "No repository matches these filters." in client.get("/repos?q=zzz").text

    # The HTMX request of the filter form gets the list only.
    part = client.get("/repos?status=error", headers={"HX-Request": "true",
                                                      "HX-Target": "repo-list"}).text
    assert 'id="repo-list"' in part[:400]
    assert "<html" not in part and 'id="repo-filters"' not in part
    assert 'hx-get="/repos?status=error"' in part


def test_repo_list_empty_state_names_the_next_action(app_db) -> None:
    client, _ = app_db
    page = client.get("/repos").text
    assert "No repositories yet." in page and 'action="/run/scan"' in page
    assert "0 shown of 0" in page


def test_running_filter_and_pill(app_db, inventory: dict[str, int]) -> None:
    client, _ = app_db
    jid = db.start_job("pull")
    progress.start(jid, "pull", "Pull mirrors", 5)
    progress.begin_item(jid, "octo/beta")
    try:
        page = client.get("/repos?status=running").text
        assert _names(page) == ["octo/beta"]
        assert '<span class="pill running">running</span>' in page
        assert "every 2s" in page                       # a job runs: the list polls
    finally:
        progress.finish(jid)
        db.finish_job(jid, "ok", 0, 0, "", "")


def test_bulk_actions(app_db, inventory: dict[str, int],
                      monkeypatch: pytest.MonkeyPatch) -> None:
    client, token = app_db
    headers = {"X-CSRF-Token": token, "HX-Request": "true", "HX-Target": "repo-list"}
    ids = [inventory["octo/alpha"], inventory["octo/beta"]]

    resp = client.post("/repos/bulk", data={"ids": ids, "action": "disable", "q": "octo"},
                       headers=headers)
    assert resp.status_code == 200
    assert "Backup turned off for 2 repositories." in resp.text
    assert "friend/tool" not in _names(resp.text)       # the filter is kept
    assert repo("octo/alpha")["enabled"] == 0 and repo("octo/beta")["enabled"] == 0
    assert repo("octo/delta")["enabled"] == 1

    client.post("/repos/bulk", data={"ids": ids, "action": "enable"}, headers=headers)
    assert repo("octo/alpha")["enabled"] == 1 and repo("octo/beta")["enabled"] == 1

    resp = client.post("/repos/bulk", data={"action": "enable"}, headers=headers)
    assert "Select one repository or more first." in resp.text

    started: list[tuple] = []
    monkeypatch.setattr(scheduler, "run_in_background", lambda func, *args: started.append(args))
    resp = client.post("/repos/bulk", data={"ids": ids, "action": "sync"}, headers=headers)
    assert "Sync started for 2 repositories." in resp.text
    assert len(started) == 1 and started[0][1] == sorted(ids)
    assert resp.text.count('<span class="pill running">running</span>') == 2
    jid = started[0][0]
    assert progress.snapshot(jid)["label"] == "Sync 2 selected repositories"
    jobs.run_selected_sync(*started[0])                 # no remotes: both fail, job is closed
    assert db.get_job(jid)["status"] == "error"
    assert jobs._ACTIVE == set() and progress.snapshot(jid) is None

    # Without the CSRF token the action is refused.
    assert client.post("/repos/bulk", data={"ids": ids, "action": "disable"}).status_code == 403


def test_sync_one_repository(app_db, remotes: Remotes,
                             monkeypatch: pytest.MonkeyPatch) -> None:
    client, token = app_db
    remotes.make("octo/alpha")
    repo_id = add_repo("octo/alpha")
    started: list[tuple] = []
    monkeypatch.setattr(scheduler, "run_in_background", lambda func, *args: started.append(args))
    headers = {"X-CSRF-Token": token, "HX-Request": "true", "HX-Target": f"repo-{repo_id}"}

    resp = client.post(f"/repos/{repo_id}/sync", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["HX-Trigger"] == "repo-sync-started"
    assert f'<tr id="repo-{repo_id}">' in resp.text
    assert '<span class="pill running">running</span>' in resp.text

    # A second request while the first is reserved is refused.
    again = client.post(f"/repos/{repo_id}/sync", headers=headers)
    assert "A sync of this repository is running now." in again.text
    assert "HX-Trigger" not in again.headers
    assert len(started) == 1

    # The detail page shows the running state and polls.
    detail = client.get(f"/repos/{repo_id}").text
    assert f'hx-get="/repos/{repo_id}/live"' in detail and 'hx-trigger="every 2s"' in detail

    jobs.run_selected_sync(*started[0])
    job = db.get_job(started[0][0])
    assert (job["job_type"], job["status"]) == ("pull", "ok")
    assert job["summary"] == "Synced octo/alpha"
    assert repo("octo/alpha")["last_status"] == "ok"
    assert db.history_snapshots() == []                 # only the full pull job adds a point

    live = client.get(f"/repos/{repo_id}/live", headers={"HX-Request": "true"})
    assert live.headers["HX-Refresh"] == "true"
    assert "hx-trigger" not in live.text


def test_sync_one_is_refused_while_the_pull_job_runs(app_db,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    client, token = app_db
    repo_id = add_repo("octo/alpha")
    started: list[tuple] = []
    monkeypatch.setattr(scheduler, "run_in_background", lambda func, *args: started.append(args))
    lock = jobs._LOCKS["pull"]
    assert lock.acquire(blocking=False)
    try:
        resp = client.post(f"/repos/{repo_id}/sync",
                           headers={"X-CSRF-Token": token, "HX-Request": "true"})
        assert "The pull job is running now." in resp.text
        # The plain form of the detail page gets the same message after the redirect.
        resp = client.post(f"/repos/{repo_id}/sync",
                           data={"csrf_token": token, "back": "detail"})
        assert resp.status_code == 303 and resp.headers["location"] == f"/repos/{repo_id}"
        assert "The pull job is running now." in client.get(f"/repos/{repo_id}").text
    finally:
        lock.release()
    assert started == []
    assert db.recent_jobs() == []                       # no job run was recorded


def test_repo_detail_page(app_db, remotes: Remotes) -> None:
    client, token = app_db
    remotes.make("octo/alpha")
    repo_id = add_repo("octo/alpha")
    page = client.get(f"/repos/{repo_id}").text
    assert "No sync yet." in page and "No mirror on disk yet." in page
    assert "hx-trigger" not in page

    jobs.run_pull_job()
    page = client.get(f"/repos/{repo_id}").text
    assert "value='git clone \"" in page and 'data-copy="#restore-cmd"' in page
    assert "octo" in page and "alpha.git" in page
    assert '<td class="mono-cell">clone</td>' in page
    assert "Sync now" in page and "Turn backup off" in page

    resp = client.post(f"/repos/{repo_id}/toggle", data={"csrf_token": token, "back": "detail"})
    assert resp.headers["location"] == f"/repos/{repo_id}"
    assert "Turn backup on" in client.get(f"/repos/{repo_id}").text
    assert client.get("/repos/99999").status_code == 303


# --- dashboard and job page ------------------------------------------------------

def test_dashboard_idle_and_running(app_db, inventory: dict[str, int]) -> None:
    client, _ = app_db
    page = client.get("/").text
    assert "every 2s" not in page
    assert 'aria-live="polite"' in page
    assert "<b>1</b> failing" in page and "<b>1</b> gone upstream" in page
    assert "Needs attention" in page and f'href="/repos/{inventory["octo/beta"]}"' in page
    assert "The chart needs 2 completed pull jobs." in page
    idle = client.get("/live", headers={"HX-Request": "true"})
    assert idle.headers["HX-Refresh"] == "true"

    jid = db.start_job("pull")
    progress.start(jid, "pull", "Pull mirrors", 4)
    progress.begin_item(jid, "octo/alpha")
    progress.end_item(jid, "octo/beta", "error", "[error] octo/beta — clone failed")
    try:
        page = client.get("/").text
        assert 'hx-get="/live" hx-trigger="every 2s"' in page
        assert "Running now" in page
        assert 'aria-valuenow="1"' in page and 'aria-valuemax="4"' in page
        assert '<span class="chip">octo/alpha</span>' in page
        assert "[error] octo/beta" in page
        live = client.get("/live", headers={"HX-Request": "true"})
        assert "HX-Refresh" not in live.headers
        assert "<html" not in live.text and "every 2s" in live.text

        job_page = client.get(f"/jobs/{jid}").text
        assert f'hx-get="/jobs/{jid}/live" hx-trigger="every 2s"' in job_page
        assert "[error] octo/beta" in job_page
        part = client.get(f"/jobs/{jid}/live", headers={"HX-Request": "true"})
        assert "HX-Refresh" not in part.headers and "<html" not in part.text
    finally:
        progress.finish(jid)
        db.finish_job(jid, "ok", 0, 0, "done", "")
    done = client.get(f"/jobs/{jid}/live", headers={"HX-Request": "true"})
    assert done.headers["HX-Refresh"] == "true"
    assert "hx-trigger" not in client.get(f"/jobs/{jid}").text


def test_dashboard_all_good_and_chart(app_db, remotes: Remotes) -> None:
    client, _ = app_db
    remotes.make("octo/alpha")
    add_repo("octo/alpha")
    jobs.run_pull_job()
    jobs.run_pull_job()
    page = client.get("/").text
    assert "All good." in page
    assert '<svg class="chart"' in page
    assert "No failed or stale repository." in page

    job = db.recent_jobs(1)[0]
    job_page = client.get(f"/jobs/{job['id']}").text
    assert "<h2>Repositories</h2>" in job_page
    assert '<td class="mono-cell">update</td>' in job_page
    assert "[ok] octo/alpha" in job_page


def test_stale_repository_needs_attention(app_db) -> None:
    client, _ = app_db
    old = time.time() - 5 * 86400
    add_repo("octo/old", last_status="ok", last_sync_at=old, last_ok_at=old)
    page = client.get("/").text
    assert "<b>1</b> stale" in page
    assert '<span class="pill skipped">stale</span>' in page


def test_run_now_redirects(app_db, monkeypatch: pytest.MonkeyPatch) -> None:
    client, token = app_db
    fired: list[str] = []
    monkeypatch.setattr(scheduler, "trigger_now", fired.append)
    resp = client.post("/run/verify", headers={"X-CSRF-Token": token, "HX-Request": "true"})
    assert resp.status_code == 204 and resp.headers["HX-Redirect"] == "/"
    assert fired == ["verify"]


# --- settings --------------------------------------------------------------------

def test_pull_workers_setting(app_db) -> None:
    client, token = app_db
    assert jobs.pull_workers() == 4
    page = client.get("/settings").text
    assert "<h2>Sync</h2>" in page and 'name="pull_workers"' in page
    assert 'name="schedule_verify"' in page

    def save(value: str) -> int:
        client.post("/settings", data={"csrf_token": token, "pull_workers": value})
        return jobs.pull_workers()

    assert save("6") == 6
    assert save("99") == 8
    assert save("-3") == 1
    assert save("many") == 1                            # not a number: the value is kept


def test_github_listing_has_the_numeric_id(app_db, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": 4242, "full_name": "octo/alpha",
                                          "private": False, "default_branch": "main"}])

    real_client = httpx.Client
    monkeypatch.setattr(github.runtime, "github_token", lambda: "test-token")
    monkeypatch.setattr(github.httpx, "Client", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), base_url=kw["base_url"]))
    repos = github.list_all_repos(include_forks=True, include_archived=True)
    assert repos[0]["id"] == 4242 and repos[0]["full_name"] == "octo/alpha"


def test_protection_job_does_nothing_when_the_copy_is_skipped(
        app_db, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(jobs.protection, "run_protection_copy",
                        lambda delete: called.append(delete) or (True, ""))
    db.set_setting("protection_skip", "1")
    jobs.run_protection_job()
    assert called == []
    assert db.recent_jobs(1)[0]["status"] == "skipped"

    db.set_setting("protection_skip", "0")
    jobs.run_protection_job()
    assert len(called) == 1
    assert db.recent_jobs(1)[0]["status"] == "ok"
