"""Token check, setup wizard, first-run redirect, settings sections, auto_enable_new.

No test uses the network: `app.github._transport` is an `httpx.MockTransport`.
"""
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, github, jobs, runtime, scheduler, setup_state
from app.config import get_settings
from app.routes import setup as setup_routes

GOOD = "ghp_goodclassictoken"
GOOD_FG = "github_pat_goodfinegrained"
NO_REPO_SCOPE = "ghp_norepo"
SOON = "ghp_expiressoon"

REPOS = [
    {"full_name": "octo/alpha", "private": False, "fork": False, "archived": False,
     "default_branch": "main", "owner": {"login": "octo"}, "description": "First one",
     "size": 120, "pushed_at": "2026-10-01T10:00:00Z", "has_wiki": True},
    {"full_name": "octo/secret", "private": True, "fork": False, "archived": False,
     "default_branch": "main", "owner": {"login": "octo"}, "description": None,
     "size": 5, "pushed_at": "2026-09-01T10:00:00Z"},
    {"full_name": "acme/forked", "private": False, "fork": True, "archived": True,
     "default_branch": "trunk", "owner": {"login": "acme"}, "description": "A fork",
     "size": 0, "pushed_at": None},
]


def _expiry(days: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(time.time() + days * 86400))


class FakeGitHub:
    """Answers `/user` and `/user/repos` like the GitHub API. Counts calls."""

    def __init__(self) -> None:
        self.repos = list(REPOS)
        self.calls: list[str] = []
        self.offline = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if self.offline:
            raise httpx.ConnectError("no route to host", request=request)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in (GOOD, GOOD_FG, NO_REPO_SCOPE, SOON):
            return httpx.Response(401, json={"message": "Bad credentials"})
        if request.url.path == "/user":
            headers = {}
            if token == GOOD:
                headers = {"X-OAuth-Scopes": "repo, read:org",
                           "github-authentication-token-expiration": _expiry(200)}
            elif token == NO_REPO_SCOPE:
                headers = {"X-OAuth-Scopes": "read:org"}
            elif token == SOON:
                headers = {"X-OAuth-Scopes": "repo",
                           "github-authentication-token-expiration": _expiry(3.5)}
            return httpx.Response(200, json={"login": "octo", "name": "Octo Cat"},
                                  headers=headers)
        if request.url.path == "/user/repos":
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=self.repos if page == 1 else [])
        return httpx.Response(404, json={})


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHub]:
    fake = FakeGitHub()
    monkeypatch.setattr(github, "_transport", httpx.MockTransport(fake))
    setup_routes._list_cache.clear()
    yield fake
    setup_routes._list_cache.clear()


@pytest.fixture
def triggered(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record job triggers instead of running git."""
    calls: list[str] = []
    monkeypatch.setattr(scheduler, "trigger_now", calls.append)
    return calls


@pytest.fixture
def first_run(authed: tuple[TestClient, str]) -> tuple[TestClient, str]:
    """A signed-in client on an install where setup is not complete."""
    db.set_setting("setup_complete", "0")
    return authed


# --- token check ------------------------------------------------------------

def test_check_token_classic(gh: FakeGitHub, client: TestClient) -> None:
    check = github.check_token(GOOD)
    assert check.ok and check.login == "octo" and check.name == "Octo Cat"
    assert check.token_type == "classic"
    assert check.scopes == ["repo", "read:org"]
    assert check.expires_at and 199 <= setup_state.days_left(check.expires_at) <= 200
    assert setup_state.token_warnings(check.__dict__) == []


def test_check_token_fine_grained_has_no_scopes_or_expiry(gh: FakeGitHub,
                                                          client: TestClient) -> None:
    check = github.check_token(GOOD_FG)
    assert check.ok and check.token_type == "fine-grained"
    assert check.scopes is None and check.expires_at is None
    assert setup_state.token_warnings(check.__dict__) == []


def test_check_token_warnings(gh: FakeGitHub, client: TestClient) -> None:
    no_repo = setup_state.token_warnings(github.check_token(NO_REPO_SCOPE).__dict__)
    assert len(no_repo) == 1 and "Private repositories will not be visible" in no_repo[0]
    soon = setup_state.token_warnings(github.check_token(SOON).__dict__)
    assert len(soon) == 1 and "expires in 3 days" in soon[0]


def test_check_token_errors_are_plain(gh: FakeGitHub, client: TestClient) -> None:
    bad = github.check_token("ghp_wrong")
    assert not bad.ok and bad.error_kind == "rejected"
    assert bad.error == "GitHub rejected the token. The token is wrong or expired."
    gh.offline = True
    down = github.check_token(GOOD)
    assert not down.ok and down.error_kind == "network"
    assert "cannot reach api.github.com from the container" in down.error


def test_check_token_does_not_send_a_malformed_token(gh: FakeGitHub, client: TestClient) -> None:
    for raw in ("", "ghp_two words", "ghp_line\nbreak", "ghp_café"):
        check = github.check_token(raw)
        assert not check.ok and check.error_kind == "rejected"
    assert gh.calls == []


def test_expiry_header_formats() -> None:
    assert github._parse_expiry("2026-12-31 23:59:59 UTC") == 1798761599.0
    assert github._parse_expiry("2026-12-31 16:59:59 -0700") == 1798761599.0
    assert github._parse_expiry("soon") is None
    assert github._parse_expiry(None) is None


def test_list_all_repos_keeps_old_keys_and_adds_new(gh: FakeGitHub, client: TestClient) -> None:
    runtime.store_github_token(GOOD)
    repos = github.list_all_repos(include_forks=True, include_archived=True)
    assert [r["full_name"] for r in repos] == ["octo/alpha", "octo/secret", "acme/forked"]
    # "id" is the numeric GitHub id; this fake API sends none.
    assert {k: v for k, v in repos[1].items() if k != "id"} == {
        "full_name": "octo/secret", "private": 1, "fork": 0, "archived": 0,
        "default_branch": "main", "has_wiki": False, "owner": "octo",
        "description": "", "size_kb": 5, "pushed_at": "2026-09-01T10:00:00Z",
    }
    assert len(github.list_all_repos(include_forks=False, include_archived=True)) == 2


# --- token storage ----------------------------------------------------------

def test_stored_token_wins_over_env_and_is_encrypted(client: TestClient,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_from_env")
    get_settings.cache_clear()
    assert runtime.github_token() == "ghp_from_env" and runtime.token_source() == "env"
    runtime.store_github_token(GOOD)
    assert runtime.github_token() == GOOD and runtime.token_source() == "stored"
    assert GOOD not in "".join(db.get_all_settings().values())
    runtime.remove_stored_github_token()
    assert runtime.github_token() == "ghp_from_env"


def test_changed_secret_key_makes_the_token_not_set(authed: tuple[TestClient, str],
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = authed
    runtime.store_github_token(GOOD)
    monkeypatch.setenv("SECRET_KEY", "f" * 48)
    get_settings.cache_clear()
    assert runtime.github_token() == ""
    assert runtime.stored_token_unreadable() and runtime.token_source() == "none"
    message = "The stored token cannot be read because SECRET_KEY changed. Enter the token again."
    for path in ("/settings", "/setup/token", "/"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert message in resp.text, path


def test_storage_roots_come_from_settings_then_env(client: TestClient, tmp_path: Path) -> None:
    assert runtime.mirror_root() == tmp_path / "mirrors"
    assert runtime.protection_root() == tmp_path / "protection"
    db.set_setting("mirror_root", str(tmp_path / "picked"))
    db.set_setting("protection_root", str(tmp_path / "copy"))
    assert runtime.mirror_root() == tmp_path / "picked"
    assert runtime.protection_root() == tmp_path / "copy"


# --- first run --------------------------------------------------------------

def test_first_run_redirects_to_setup(first_run: tuple[TestClient, str]) -> None:
    client, _ = first_run
    resp = client.get("/")
    assert resp.status_code == 303 and resp.headers["location"] == "/setup"
    resp = client.get("/setup")
    assert resp.status_code == 303 and resp.headers["location"] == "/setup/storage"
    assert client.get("/setup/storage").status_code == 200


def test_existing_install_is_not_sent_to_setup(first_run: tuple[TestClient, str],
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = first_run
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_from_env")
    get_settings.cache_clear()
    # A token alone is not enough ...
    assert client.get("/").status_code == 303
    # ... a token and one repository row is an install that was in use.
    db.upsert_repo({"full_name": "octo/old", "private": 0, "fork": 0, "archived": 0,
                    "default_branch": "main", "mirror_path": "octo/old.git",
                    "discovered_at": 1.0})
    assert client.get("/").status_code == 200


def test_setup_pages_need_a_session(client: TestClient) -> None:
    for path in ("/setup", "/setup/storage", "/setup/token", "/setup/repos", "/setup/start",
                 "/browse?field=mirror_root"):
        resp = client.get(path)
        assert resp.status_code == 303 and resp.headers["location"] == "/login", path


# --- folder browser routes --------------------------------------------------

def test_picker_lists_folders_and_refuses_traversal(authed: tuple[TestClient, str],
                                                    tmp_path: Path) -> None:
    client, token = authed
    (tmp_path / "nas" / "git").mkdir(parents=True)
    (tmp_path / "nas" / "file.txt").write_text("x")
    html = client.get("/browse", params={"field": "mirror_root",
                                         "path": str(tmp_path / "nas")}).text
    assert ">git</button>" in html and "file.txt" not in html
    assert "Use this folder" in html and "New folder" in html

    # Root list when no path is given.
    html = client.get("/browse", params={"field": "mirror_root", "path": ""}).text
    assert "Use this folder" not in html

    outside = tmp_path.parent
    html = client.get("/browse", params={"field": "mirror_root",
                                         "path": str(tmp_path / "nas" / ".." / "..")}).text
    assert "outside the folders that the app can browse" in html
    assert "Use this folder" not in html
    for entry in outside.iterdir():
        if entry.is_dir() and entry != tmp_path:
            assert f">{entry.name}</button>" not in html

    # "Browse" sends the field value under the field name; a missing folder
    # opens the nearest parent with a message.
    html = client.get("/browse", params={"field": "mirror_root",
                                         "mirror_root": str(tmp_path / "nas" / "nope")}).text
    assert "does not exist" in html and ">git</button>" in html

    assert client.get("/browse", params={"field": "evil"}).status_code == 400
    headers = {"X-CSRF-Token": token}
    resp = client.post("/browse/use", headers=headers, data={
        "field": "mirror_root", "path": str(outside), "mirror_root": "keep-me"})
    assert "outside the folders" in resp.text and 'value="keep-me"' in resp.text


def test_picker_new_folder_and_use(authed: tuple[TestClient, str], tmp_path: Path) -> None:
    client, token = authed
    headers = {"X-CSRF-Token": token}
    base = tmp_path / "nas"
    base.mkdir()
    assert client.post("/browse/mkdir", data={
        "field": "mirror_root", "path": str(base), "name": "x"}).status_code == 403

    html = client.post("/browse/mkdir", headers=headers, data={
        "field": "mirror_root", "path": str(base), "name": "mirrors"}).text
    assert (base / "mirrors").is_dir() and 'aria-current="location">mirrors<' in html

    html = client.post("/browse/mkdir", headers=headers, data={
        "field": "mirror_root", "path": str(base), "name": "../escape"}).text
    assert "must not contain" in html and not (tmp_path / "escape").exists()

    html = client.post("/browse/use", headers=headers, data={
        "field": "mirror_root", "path": str(base / "mirrors"),
        "protection_root": str(base)}).text
    assert 'id="ff-mirror_root"' in html
    assert "Free space" in html and "Write test: OK" in html
    assert "inside the other" in html
    assert list((base / "mirrors").iterdir()) == []


# --- wizard -----------------------------------------------------------------

def test_wizard_end_to_end(first_run: tuple[TestClient, str], gh: FakeGitHub,
                           triggered: list[str], tmp_path: Path) -> None:
    client, token = first_run
    form = {"csrf_token": token}
    mirror = tmp_path / "nas" / "git-mirrors"
    copy = tmp_path / "nas2" / "git-protection"

    # Step 1: storage. A folder outside the browse roots is refused.
    page = client.get("/setup/storage").text
    assert 'aria-current="step"' in page and str(tmp_path / "mirrors") in page
    resp = client.post("/setup/storage", data={
        **form, "mirror_root": str(tmp_path.parent), "protection_root": str(copy)})
    assert resp.status_code == 400 and "outside the folders" in resp.text
    assert db.get_setting("mirror_root") is None
    resp = client.post("/setup/storage", data={
        **form, "mirror_root": str(mirror), "protection_root": str(copy)})
    assert resp.status_code == 303 and resp.headers["location"] == "/setup/token"
    assert runtime.mirror_root() == mirror.resolve() and mirror.is_dir()
    assert runtime.protection_root() == copy.resolve()
    assert db.get_setting("schedule_protection") == "0 5 * * 0"

    # Step 2: token. "Check token" shows the account and stores nothing.
    assert "Enter the token" in client.get("/setup/token").text
    html = client.post("/setup/token/check", headers={"X-CSRF-Token": token},
                       data={"token": GOOD}).text
    assert "octo" in html and "Classic" in html and "repo, read:org" in html
    assert GOOD not in html and runtime.github_token() == ""
    # An empty or rejected token is not stored.
    assert client.post("/setup/token", data={**form, "token": ""}).status_code == 400
    resp = client.post("/setup/token", data={**form, "token": "ghp_wrong"})
    assert resp.status_code == 400 and "The token was not stored." in resp.text
    assert "ghp_wrong" not in resp.text and runtime.github_token() == ""
    resp = client.post("/setup/token", data={**form, "token": GOOD})
    assert resp.status_code == 303 and resp.headers["location"] == "/setup/repos"
    assert runtime.github_token() == GOOD
    page = client.get("/setup/token").text
    assert "A token is stored in the app." in page and GOOD not in page

    # Step 3: repositories, live from the (fake) API.
    assert 'hx-get="/setup/repos/list"' in client.get("/setup/repos").text
    html = client.get("/setup/repos/list").text
    for name in ("octo/alpha", "octo/secret", "acme/forked"):
        assert f'value="{name}"' in html
    assert "3 of 3 selected" in html and '<option value="acme">' in html
    calls_before = gh.calls.count("/user/repos")
    resp = client.post("/setup/repos", data={**form, "repo": ["octo/alpha", "octo/secret"]})
    assert resp.status_code == 303 and resp.headers["location"] == "/setup/start"
    assert gh.calls.count("/user/repos") == calls_before  # the cached list is used
    rows = {r["full_name"]: r for r in db.list_repos()}
    assert {n: r["enabled"] for n, r in rows.items()} == {
        "octo/alpha": 1, "octo/secret": 1, "acme/forked": 0}
    assert rows["octo/secret"]["private"] == 1 and rows["acme/forked"]["fork"] == 1
    assert rows["acme/forked"]["mirror_path"] == "acme/forked.git"
    # Opened again, the list shows the stored choice.
    assert "2 of 3 selected" in client.get("/setup/repos/list").text

    # Step 4: summary and start. Setup is complete only after "Start".
    page = client.get("/setup/start").text
    assert str(mirror.resolve()) in page and "octo" in page
    assert "2 selected for backup, 3 in the list" in page
    assert db.get_setting("setup_complete") == "0"
    resp = client.post("/setup/start", data=form)
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    assert triggered == ["pull"]
    assert db.get_setting("setup_complete") == "1"
    assert client.get("/").status_code == 200
    # The wizard did not change the discovery setting.
    assert db.get_bool("auto_enable_new") is True


def test_wizard_skip_protection_empties_the_schedule(first_run: tuple[TestClient, str],
                                                     tmp_path: Path) -> None:
    client, token = first_run
    data = {"csrf_token": token, "mirror_root": str(tmp_path / "m"),
            "protection_root": "", "skip_protection": "1"}
    assert client.post("/setup/storage", data=data).status_code == 303
    assert db.get_setting("schedule_protection") == ""
    assert scheduler.next_runs()["protection"] is None
    assert db.get_setting("protection_root") is None
    assert "Skipped." in client.get("/setup/start").text
    # Setup run again without "skip": the default schedule comes back.
    data = {"csrf_token": token, "mirror_root": str(tmp_path / "m"),
            "protection_root": str(tmp_path / "p")}
    assert client.post("/setup/storage", data=data).status_code == 303
    assert db.get_setting("schedule_protection") == "0 5 * * 0"
    assert scheduler.next_runs()["protection"] is not None


def test_wizard_warns_when_folders_overlap(first_run: tuple[TestClient, str],
                                           tmp_path: Path) -> None:
    client, token = first_run
    data = {"csrf_token": token, "mirror_root": str(tmp_path / "m"),
            "protection_root": str(tmp_path / "m" / "copy")}
    assert client.post("/setup/storage", data=data).status_code == 303  # a warning, not a block
    assert "inside the other" in client.get("/setup/storage").text
    assert "inside the other" in client.get("/setup/start").text


def test_wizard_repo_step_reports_api_errors(first_run: tuple[TestClient, str],
                                             gh: FakeGitHub, triggered: list[str]) -> None:
    client, token = first_run
    # No token: step 3 sends the user back to step 2, and "Start" does nothing.
    resp = client.get("/setup/repos")
    assert resp.status_code == 303 and resp.headers["location"] == "/setup/token"
    resp = client.post("/setup/start", data={"csrf_token": token})
    assert resp.headers["location"] == "/setup/token" and triggered == []
    assert db.get_setting("setup_complete") == "0"

    runtime.store_github_token(GOOD)
    gh.offline = True
    assert "cannot reach api.github.com" in client.get("/setup/repos/list").text
    resp = client.post("/setup/repos", data={"csrf_token": token, "repo": ["octo/alpha"]})
    assert resp.status_code == 502 and db.count_repos() == 0
    gh.offline = False
    runtime.store_github_token("ghp_revoked")
    assert "GitHub rejected the token" in client.get("/setup/repos/list").text


def test_wizard_works_with_the_env_token(first_run: tuple[TestClient, str], gh: FakeGitHub,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    client, token = first_run
    monkeypatch.setenv("GITHUB_TOKEN", GOOD)
    get_settings.cache_clear()
    assert "GITHUB_TOKEN value in the .env file" in client.get("/setup/token").text
    resp = client.post("/setup/token", data={"csrf_token": token, "token": ""})
    assert resp.status_code == 303 and resp.headers["location"] == "/setup/repos"
    assert db.get_setting("github_token_enc") is None


# --- settings sections ------------------------------------------------------

def test_settings_token_replace_and_remove(authed: tuple[TestClient, str],
                                           gh: FakeGitHub) -> None:
    client, token = authed
    form = {"csrf_token": token}
    page = client.get("/settings").text
    assert "No GitHub token is set." in page and "Run setup again" in page
    assert "Remove stored token" not in page

    resp = client.post("/settings/token", data={**form, "token": "ghp_wrong"})
    assert resp.status_code == 400 and "The token was not stored." in resp.text
    resp = client.post("/settings/token", data={**form, "token": GOOD_FG})
    assert resp.status_code == 303
    page = client.get(resp.headers["location"]).text
    assert "Token checked and stored." in page and "Fine-grained" in page and "octo" in page
    assert GOOD_FG not in page and "Remove stored token" in page
    stored = db.get_setting("github_token_enc")
    assert stored and stored not in page

    # Removal needs the confirm field of the form.
    client.post("/settings/token/remove", data=form)
    assert runtime.github_token() == GOOD_FG
    client.post("/settings/token/remove", data={**form, "confirm": "yes"})
    assert runtime.github_token() == "" and setup_state.load_token_check() is None


def test_settings_storage_change_warns_about_existing_mirrors(
        authed: tuple[TestClient, str], tmp_path: Path) -> None:
    client, token = authed
    form = {"csrf_token": token}
    page = client.get("/settings").text
    assert 'id="ff-mirror_root"' in page and 'id="ff-protection_root"' in page

    resp = client.post("/settings/storage", data={
        **form, "mirror_root": str(tmp_path.parent / "x"),
        "protection_root": str(tmp_path / "p")})
    assert resp.status_code == 400 and "outside the folders" in resp.text
    assert db.get_setting("mirror_root") is None

    db.upsert_repo({"full_name": "octo/alpha", "private": 0, "fork": 0, "archived": 0,
                    "default_branch": "main", "mirror_path": "octo/alpha.git",
                    "discovered_at": 1.0})
    db.record_repo_result("octo/alpha", "ok", None, 10)
    old_file = tmp_path / "mirrors" / "octo" / "alpha.git" / "HEAD"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("ref: refs/heads/main\n")

    resp = client.post("/settings/storage", data={
        **form, "mirror_root": str(tmp_path / "new"), "protection_root": str(tmp_path / "p")})
    assert resp.status_code == 303 and "moved=1" in resp.headers["location"]
    page = client.get(resp.headers["location"]).text
    assert "The app did not move data" in page
    assert runtime.mirror_root() == (tmp_path / "new").resolve()
    assert old_file.exists()  # nothing was moved or deleted


def test_auto_enable_new_checkbox_is_saved(authed: tuple[TestClient, str]) -> None:
    client, token = authed
    assert "Turn on backup for newly found repositories" in client.get("/settings").text
    assert db.get_bool("auto_enable_new") is True  # default on
    client.post("/settings", data={"csrf_token": token})
    assert db.get_bool("auto_enable_new") is False
    client.post("/settings", data={"csrf_token": token, "auto_enable_new": "on"})
    assert db.get_bool("auto_enable_new") is True


# --- scan job ---------------------------------------------------------------

def test_scan_respects_auto_enable_new_for_new_rows_only(client: TestClient,
                                                         gh: FakeGitHub) -> None:
    runtime.store_github_token(GOOD)
    db.set_setting("include_forks", "1")
    db.set_setting("include_archived", "1")
    gh.repos = REPOS[:1]
    jobs.run_scan_job()
    assert {r["full_name"]: r["enabled"] for r in db.list_repos()} == {"octo/alpha": 1}

    db.set_setting("auto_enable_new", "0")
    gh.repos = list(REPOS)
    jobs.run_scan_job()
    assert {r["full_name"]: r["enabled"] for r in db.list_repos()} == {
        "octo/alpha": 1, "octo/secret": 0, "acme/forked": 0}

    # Back on: rows that exist keep their value.
    db.set_setting("auto_enable_new", "1")
    jobs.run_scan_job()
    assert {r["full_name"]: r["enabled"] for r in db.list_repos()} == {
        "octo/alpha": 1, "octo/secret": 0, "acme/forked": 0}


def test_scan_refreshes_token_health_for_the_dashboard(authed: tuple[TestClient, str],
                                                       gh: FakeGitHub) -> None:
    client, _ = authed
    # No token: the dashboard says so.
    assert "No GitHub token is set." in client.get("/").text

    runtime.store_github_token(GOOD)
    jobs.run_scan_job()
    assert setup_state.load_token_check()["login"] == "octo"
    assert "token-health" not in client.get("/").text  # nothing to act on

    runtime.store_github_token(SOON)
    jobs.run_scan_job()
    assert "The GitHub token expires in 3 days." in client.get("/").text

    # A network failure keeps the last result.
    gh.offline = True
    jobs.run_scan_job()
    assert setup_state.load_token_check()["ok"] is True
    gh.offline = False

    runtime.store_github_token("ghp_revoked")
    jobs.run_scan_job()
    page = client.get("/").text
    assert "GitHub rejected the token at the last check." in page
    assert "ghp_revoked" not in page
    assert "ghp_revoked" not in (db.recent_jobs(1)[0]["detail"] or "")
