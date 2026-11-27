"""Watched repositories: add flows, watched owners, scan job, source rules.

GitHub is always an `httpx.MockTransport`. No test uses the network.
"""
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, github, jobs, runtime, watch

Handler = Callable[[httpx.Request], httpx.Response]
GH = Callable[[Handler], list[httpx.Request]]
Authed = tuple[TestClient, str]


def repo_json(full_name: str, **extra: object) -> dict:
    return {"full_name": full_name, "private": False, "fork": False, "archived": False,
            "default_branch": "main", "has_wiki": True, **extra}


def repo_row(full_name: str, **extra: object) -> dict:
    return {"full_name": full_name, "private": 0, "fork": 0, "archived": 0,
            "default_branch": "main", "mirror_path": f"{full_name}.git",
            "discovered_at": 1.0, **extra}


@pytest.fixture
def gh(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> GH:
    """Install a fake GitHub. Returns the list that collects the requests.

    Depends on `client` so that the database exists. The token is empty
    unless a test sets one with `use_token`.
    """
    monkeypatch.setattr(runtime, "github_token", lambda: "")

    def install(handler: Handler) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        monkeypatch.setattr(github, "_transport", httpx.MockTransport(record))
        return seen

    return install


def use_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "github_token", lambda: "test-token-value")


def not_found(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"message": "Not Found"})


def rate_limited(reset: int) -> Handler:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)},
            json={"message": "API rate limit exceeded for 203.0.113.5."},
        )
    return handler


def last_job() -> dict:
    return dict(db.recent_jobs(1)[0])


# --- add one repository -------------------------------------------------------

def test_add_repo_uses_canonical_name_and_safe_defaults(gh: GH) -> None:
    seen = gh(lambda r: httpx.Response(200, json=repo_json("Octocat/Hello-World", fork=True)))
    result = watch.add_repo("https://github.com/octocat/hello-world/tree/main")

    assert result.ok and result.name == "Octocat/Hello-World"
    assert [r.url.path for r in seen] == ["/repos/octocat/hello-world"]
    assert "authorization" not in seen[0].headers  # no token: no auth header
    row = db.get_repo_by_name("octocat/hello-world")
    assert row["full_name"] == "Octocat/Hello-World"
    assert (row["source"], row["enabled"], row["metadata_enabled"]) == ("watched", 1, 0)
    assert row["fork"] == 1
    assert row["mirror_path"] == "Octocat/Hello-World.git"


def test_add_repo_sends_the_token_when_there_is_one(gh: GH,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    use_token(monkeypatch)
    seen = gh(lambda r: httpx.Response(200, json=repo_json("octo/demo")))
    assert watch.add_repo("octo/demo").ok
    assert seen[0].headers["authorization"] == "Bearer test-token-value"


def test_add_repo_follows_the_redirect_of_a_renamed_repository(gh: GH) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/old-name":
            return httpx.Response(301, headers={"Location": "/repositories/42"})
        assert request.url.path == "/repositories/42"
        return httpx.Response(200, json=repo_json("octo/new-name"))

    gh(handler)
    result = watch.add_repo("octo/old-name")
    assert result.ok and result.name == "octo/new-name"
    assert db.get_repo_by_name("octo/old-name") is None


def test_add_repo_duplicate_changes_nothing(gh: GH) -> None:
    seen = gh(lambda r: httpx.Response(200, json=repo_json("octo/demo")))
    assert watch.add_repo("octo/demo").ok
    db.toggle_repo(db.get_repo_by_name("octo/demo")["id"])

    result = watch.add_repo("git@github.com:OCTO/Demo.git")
    assert not result.ok
    assert result.message == "octo/demo is already in the list. Nothing changed."
    assert len(seen) == 1  # the duplicate is found before a GitHub call
    assert len(db.list_repos()) == 1
    assert db.get_repo_by_name("octo/demo")["enabled"] == 0


def test_add_repo_duplicate_found_by_canonical_name(gh: GH) -> None:
    db.upsert_repo(repo_row("octo/new-name"))
    gh(lambda r: httpx.Response(200, json=repo_json("octo/new-name")))
    result = watch.add_repo("octo/old-name")
    assert not result.ok and "already in the list" in result.message
    rows = db.list_repos()
    assert len(rows) == 1 and rows[0]["source"] == "mine"


def test_add_repo_not_found(gh: GH) -> None:
    gh(not_found)
    result = watch.add_repo("octo/missing")
    assert not result.ok
    assert result.message == ("The repository octo/missing does not exist, or it is "
                              "private and not visible with the current token.")
    assert db.list_repos() == []


def test_add_repo_rate_limit_without_token(gh: GH) -> None:
    reset = int(time.time()) + 1800
    gh(rate_limited(reset))
    result = watch.add_repo("octo/demo")
    hhmm = time.strftime("%H:%M", time.localtime(reset))
    assert not result.ok
    assert result.message == (f"GitHub rate limit reached. It resets at {hhmm}. "
                              "Add a token in Settings to raise the limit.")
    assert db.list_repos() == []


def test_rate_limit_with_token_does_not_ask_for_a_token(gh: GH,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    use_token(monkeypatch)
    gh(rate_limited(int(time.time()) + 60))
    with pytest.raises(github.RateLimited) as err:
        github.get_repo("octo", "demo")
    assert "Add a token" not in str(err.value)
    assert "test-token-value" not in str(err.value)


def test_secondary_rate_limit_uses_retry_after(gh: GH) -> None:
    gh(lambda r: httpx.Response(429, headers={"Retry-After": "120"}, json={"message": "slow"}))
    before = time.time()
    with pytest.raises(github.RateLimited) as err:
        github.get_repo("octo", "demo")
    assert before + 119 <= err.value.reset_at <= time.time() + 121


def test_forbidden_without_rate_limit_is_not_called_a_rate_limit(gh: GH) -> None:
    gh(lambda r: httpx.Response(403, headers={"X-RateLimit-Remaining": "41"},
                                json={"message": "Repository access blocked"}))
    with pytest.raises(github.GitHubError) as err:
        github.get_repo("octo", "demo")
    assert not isinstance(err.value, github.RateLimited)
    assert str(err.value) == "GitHub answered with HTTP 403: Repository access blocked"


def test_network_failure_has_a_plain_message(gh: GH) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    gh(handler)
    result = watch.add_repo("octo/demo")
    assert not result.ok
    assert result.message == f"{github.MSG_NETWORK} (ConnectError)"


def test_add_repo_bad_text_makes_no_github_call(gh: GH) -> None:
    seen = gh(not_found)
    result = watch.add_repo("https://gitlab.com/octo/demo")
    assert not result.ok and result.message == watch.BAD_REF_MESSAGE
    assert seen == []


def test_add_repo_refuses_an_unsafe_name_from_the_api(gh: GH) -> None:
    gh(lambda r: httpx.Response(200, json=repo_json("../../etc")))
    assert not watch.add_repo("octo/demo").ok
    assert db.list_repos() == []


# --- list_all_repos keeps its contract ---------------------------------------

def test_list_all_repos_needs_a_token(gh: GH) -> None:
    seen = gh(not_found)
    with pytest.raises(RuntimeError, match="No GitHub token"):
        github.list_all_repos(include_forks=True, include_archived=True)
    assert seen == []


def test_list_all_repos_pages_and_filters(gh: GH, monkeypatch: pytest.MonkeyPatch) -> None:
    use_token(monkeypatch)
    monkeypatch.setattr(github, "PER_PAGE", 2)
    pages = {
        "1": [repo_json("me/a", private=True), repo_json("me/b", fork=True)],
        "2": [repo_json("me/c", archived=True), repo_json("me/d")],
        "3": [],
    }
    seen = gh(lambda r: httpx.Response(200, json=pages[r.url.params["page"]]))

    everything = github.list_all_repos(include_forks=True, include_archived=True)
    assert [r["full_name"] for r in everything] == ["me/a", "me/b", "me/c", "me/d"]
    first = {k: v for k, v in everything[0].items() if k != "id"}
    assert first == {"full_name": "me/a", "private": 1, "fork": 0, "archived": 0,
                     "default_branch": "main", "has_wiki": True,
                     "owner": "me", "description": "", "size_kb": 0, "pushed_at": ""}
    assert {r.url.path for r in seen} == {"/user/repos"}
    assert seen[0].url.params["visibility"] == "all"
    assert seen[0].headers["authorization"] == "Bearer test-token-value"

    plain = github.list_all_repos(include_forks=False, include_archived=False)
    assert [r["full_name"] for r in plain] == ["me/a", "me/d"]


# --- source rules of db.upsert_repo ------------------------------------------

def test_upsert_new_rows(client: TestClient) -> None:
    assert db.upsert_repo(repo_row("me/own")) is True
    assert db.upsert_repo(repo_row("other/lib", source="watched")) is True
    own, lib = db.get_repo_by_name("me/own"), db.get_repo_by_name("other/lib")
    assert (own["source"], own["enabled"], own["metadata_enabled"]) == ("mine", 1, 1)
    assert (lib["source"], lib["enabled"], lib["metadata_enabled"]) == ("watched", 1, 0)


def test_upsert_keeps_source_and_enabled_of_an_existing_row(client: TestClient) -> None:
    db.upsert_repo(repo_row("me/own"))
    db.upsert_repo(repo_row("other/lib", source="watched"))
    for name in ("me/own", "other/lib"):
        db.toggle_repo(db.get_repo_by_name(name)["id"])  # the owner turns both off

    # A watched owner lists both again; the facts from GitHub change.
    assert db.upsert_repo(repo_row("me/own", source="watched", archived=1)) is False
    assert db.upsert_repo(repo_row("other/lib", source="watched", archived=1)) is False

    own, lib = db.get_repo_by_name("me/own"), db.get_repo_by_name("other/lib")
    assert (own["source"], own["enabled"], own["archived"]) == ("mine", 0, 1)
    assert (lib["source"], lib["enabled"], lib["archived"]) == ("watched", 0, 1)
    assert own["metadata_enabled"] == 1 and lib["metadata_enabled"] == 0


def test_upsert_watched_row_in_the_token_list_becomes_mine(client: TestClient) -> None:
    db.upsert_repo(repo_row("other/lib", source="watched"))
    db.toggle_repo(db.get_repo_by_name("other/lib")["id"])

    assert db.upsert_repo(repo_row("other/lib")) is False  # default source: mine
    row = db.get_repo_by_name("other/lib")
    assert row["source"] == "mine"
    assert row["enabled"] == 0            # still the owner's decision
    assert row["metadata_enabled"] == 0   # still the owner's decision


# --- watched owners -----------------------------------------------------------

def test_add_owner_finds_the_kind(gh: GH) -> None:
    accounts = {"/users/octocat": {"login": "octocat", "type": "User"},
                "/users/github": {"login": "GitHub", "type": "Organization"}}
    seen = gh(lambda r: httpx.Response(200, json=accounts[r.url.path]))

    assert watch.add_owner(" @octocat ", include_forks=True, include_archived=False).ok
    result = watch.add_owner("github", include_forks=False, include_archived=True)
    assert result.ok and result.name == "GitHub"

    owners = {o["login"]: dict(o) for o in db.list_watch_owners()}
    assert owners["octocat"]["kind"] == "user" and owners["GitHub"]["kind"] == "org"
    assert (owners["octocat"]["include_forks"], owners["octocat"]["include_archived"]) == (1, 0)
    assert (owners["GitHub"]["include_forks"], owners["GitHub"]["include_archived"]) == (0, 1)
    assert owners["octocat"]["last_scan_at"] is None

    # The same login in another case: refused before a GitHub call.
    again = watch.add_owner("OCTOCAT", include_forks=False, include_archived=False)
    assert not again.ok and "already a watched owner" in again.message
    assert len(seen) == 2


def test_add_owner_not_found_and_bad_login(gh: GH) -> None:
    seen = gh(not_found)
    result = watch.add_owner("nobody-here", False, False)
    assert result.message == "GitHub has no user or organisation with the name nobody-here."
    for bad in ("", "two words", "owner/name", "https://github.com/octocat", "-x", "a" * 40):
        assert watch.add_owner(bad, False, False).message == watch.BAD_LOGIN_MESSAGE
    assert len(seen) == 1
    assert db.list_watch_owners() == []


def owner_pages(pages: dict[str, dict[str, list[dict]]]) -> Handler:
    """Fake list endpoints: {path: {page number: [repo json]}}."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path not in pages:
            return httpx.Response(500, json={"message": "Server Error"})
        return httpx.Response(200, json=pages[request.url.path].get(request.url.params["page"], []))
    return handler


def test_scan_without_token_skips_my_repositories_and_scans_owners(
        gh: GH, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "PER_PAGE", 2)
    db.add_watch_owner("octocat", "user", include_forks=False, include_archived=False)
    db.add_watch_owner("bigorg", "org", include_forks=True, include_archived=True)
    seen = gh(owner_pages({
        "/users/octocat/repos": {
            "1": [repo_json("octocat/a"), repo_json("octocat/fork", fork=True)],
            "2": [repo_json("octocat/old", archived=True), repo_json("octocat/b")],
            "3": [repo_json("octocat/c")],
        },
        "/orgs/bigorg/repos": {
            "1": [repo_json("bigorg/x", fork=True), repo_json("bigorg/y", archived=True)],
            "2": [repo_json("bigorg/secret", private=True)],
        },
    }))

    jobs.run_scan_job()

    names = {r["full_name"]: r for r in db.list_repos()}
    assert set(names) == {"octocat/a", "octocat/b", "octocat/c", "bigorg/x", "bigorg/y"}
    for row in names.values():
        assert (row["source"], row["enabled"], row["metadata_enabled"]) == ("watched", 1, 0)

    calls = [(r.url.path, r.url.params["page"], r.url.params["type"]) for r in seen]
    assert calls == [
        ("/orgs/bigorg/repos", "1", "public"), ("/orgs/bigorg/repos", "2", "public"),
        ("/users/octocat/repos", "1", "owner"), ("/users/octocat/repos", "2", "owner"),
        ("/users/octocat/repos", "3", "owner"),
    ]
    assert all("authorization" not in r.headers for r in seen)

    job = last_job()
    assert job["status"] == "ok"
    assert (job["ok_count"], job["fail_count"]) == (5, 0)
    assert job["summary"] == ("Discovered 5 repositories (5 new). "
                              "My repositories: skipped, because no GitHub token is set.")
    for owner in db.list_watch_owners():
        assert owner["last_scan_at"] is not None and owner["last_error"] is None


def test_scan_without_token_and_without_owners_is_a_noted_no_op(gh: GH) -> None:
    seen = gh(not_found)
    jobs.run_scan_job()
    job = last_job()
    assert job["status"] == "ok"
    assert "skipped, because no GitHub token is set" in job["summary"]
    assert seen == []


def test_scan_one_failed_owner_does_not_fail_the_others(gh: GH) -> None:
    db.add_watch_owner("aaa-broken", "user", False, False)
    db.add_watch_owner("octocat", "user", False, False)
    db.add_watch_owner("zzz-gone", "org", False, False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/octocat/repos":
            return httpx.Response(200, json=[repo_json("octocat/a")])
        if request.url.path == "/orgs/zzz-gone/repos":
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(502, text="bad gateway")

    gh(handler)
    jobs.run_scan_job()

    assert [r["full_name"] for r in db.list_repos()] == ["octocat/a"]
    owners = {o["login"]: o for o in db.list_watch_owners()}
    assert owners["aaa-broken"]["last_error"] == "GitHub answered with HTTP 502."
    assert owners["zzz-gone"]["last_error"] == "GitHub has no organisation with the name zzz-gone."
    assert owners["octocat"]["last_error"] is None
    job = last_job()
    assert job["status"] == "partial"
    assert (job["ok_count"], job["fail_count"]) == (1, 2)
    assert "Watched owners that failed: 2." in job["summary"]
    assert "[error] watched owner aaa-broken" in job["detail"]
    assert "[ok] watched owner octocat" in job["detail"]

    # The next good scan clears the error.
    gh(lambda r: httpx.Response(200, json=[]))
    jobs.run_scan_job()
    assert all(o["last_error"] is None for o in db.list_watch_owners())
    assert last_job()["status"] == "ok"


def test_scan_stops_calling_github_after_a_rate_limit(gh: GH) -> None:
    for login in ("a-one", "b-two", "c-three"):
        db.add_watch_owner(login, "user", False, False)
    seen = gh(rate_limited(int(time.time()) + 600))

    jobs.run_scan_job()

    assert len(seen) == 1
    errors = [o["last_error"] for o in db.list_watch_owners()]
    assert all(e.startswith("GitHub rate limit reached. It resets at ") for e in errors)
    job = last_job()
    assert job["status"] == "error"  # no part of the scan was successful
    assert job["fail_count"] == 3


def test_scan_with_token_marks_own_repositories_as_mine(gh: GH,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    use_token(monkeypatch)
    db.upsert_repo(repo_row("friend/shared", source="watched"))
    db.add_watch_owner("friend", "user", False, False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user/repos":
            return httpx.Response(200, json=[repo_json("me/own", private=True),
                                             repo_json("friend/shared")])
        assert request.url.path == "/users/friend/repos"
        return httpx.Response(200, json=[repo_json("friend/shared"), repo_json("friend/lib")])

    gh(handler)
    jobs.run_scan_job()

    sources = {r["full_name"]: r["source"] for r in db.list_repos()}
    # friend/shared is in the token's list (collaborator): it becomes 'mine'
    # and the watched owner's scan after that does not change it back.
    assert sources == {"me/own": "mine", "friend/shared": "mine", "friend/lib": "watched"}
    job = last_job()
    assert job["status"] == "ok"
    assert job["summary"] == "Discovered 4 repositories (2 new)."


def test_scan_token_part_fails_and_owners_continue(gh: GH, monkeypatch: pytest.MonkeyPatch) -> None:
    use_token(monkeypatch)
    db.add_watch_owner("octocat", "user", False, False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user/repos":
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(200, json=[repo_json("octocat/a")])

    gh(handler)
    jobs.run_scan_job()

    assert [r["full_name"] for r in db.list_repos()] == ["octocat/a"]
    job = last_job()
    assert job["status"] == "partial"
    assert "My repositories: failed (GitHub rejected the token." in job["summary"]
    assert "test-token-value" not in job["summary"] + job["detail"]


# --- routes and page ----------------------------------------------------------

HX = {"HX-Request": "true"}


def test_watch_routes_need_session_and_csrf(client: TestClient, authed: Authed, gh: GH) -> None:
    seen = gh(not_found)
    c, token = authed
    assert c.post("/watch/repos", data={"repo": "octo/demo"}).status_code == 403
    assert c.post("/watch/owners", data={"login": "octo"}).status_code == 403
    c.post("/logout", data={"csrf_token": token})
    resp = c.post("/watch/repos", data={"repo": "octo/demo"}, headers={"X-CSRF-Token": token})
    assert resp.status_code in (303, 403)
    assert seen == []


def test_repos_page_shows_the_panel(authed: Authed) -> None:
    c, _token = authed
    assert "Watch a public repository" not in c.get("/repos").text   # Active tab
    html = c.get("/repos?tab=watched").text
    assert "Watch a public repository" in html
    assert 'hx-post="/watch/repos"' in html
    assert 'id="watch-result"' in html and 'aria-live="polite"' in html
    assert "Watched owners" in html and "No watched owners." in html


def test_add_repo_route_reloads_and_shows_the_row(authed: Authed, gh: GH) -> None:
    c, token = authed
    gh(lambda r: httpx.Response(200, json=repo_json("octo/demo")))
    headers = {**HX, "X-CSRF-Token": token}

    resp = c.post("/watch/repos", data={"repo": "octo/demo"}, headers=headers)
    assert resp.status_code == 204
    assert resp.headers["HX-Redirect"] == "/repos?tab=watched&watched=octo%2Fdemo"

    html = c.get(resp.headers["HX-Redirect"]).text
    assert "Added octo/demo. The next pull job makes the mirror." in html
    assert '<span class="tag tag-watched">watched</span>' in html
    assert "Stop watching octo/demo" in html
    # The notice shows only for a watched repository that is in the table.
    assert "Added" not in c.get("/repos?tab=watched&watched=evil%2Ftext").text

    resp = c.post("/watch/repos", data={"repo": "octo/demo"}, headers=headers)
    assert resp.status_code == 200
    assert "octo/demo is already in the list. Nothing changed." in resp.text
    resp = c.post("/watch/repos", data={"repo": "not a repo"}, headers=headers)
    assert resp.status_code == 200 and "This is not a repository name." in resp.text


def test_add_repo_route_shows_rate_limit_message(authed: Authed, gh: GH) -> None:
    c, token = authed
    gh(rate_limited(int(time.time()) + 900))
    resp = c.post("/watch/repos", data={"repo": "octo/demo"}, headers={**HX, "X-CSRF-Token": token})
    assert resp.status_code == 200
    assert "GitHub rate limit reached. It resets at " in resp.text
    assert "Add a token in Settings to raise the limit." in resp.text


def test_stop_watching_removes_the_row_and_keeps_the_files(authed: Authed, tmp_path: Path) -> None:
    c, token = authed
    db.upsert_repo(repo_row("other/lib", source="watched"))
    repo = db.get_repo_by_name("other/lib")
    mirror_dir = runtime.mirror_root() / "other" / "lib.git"
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    # The confirmation text in the row shows the path and what stays.
    html = c.get("/repos?tab=watched").text
    assert str(mirror_dir) in html
    assert "It does not delete the mirror." in html
    assert "You can delete them by hand." in html

    resp = c.post(f"/watch/repos/{repo['id']}/stop", headers={**HX, "X-CSRF-Token": token})
    assert resp.status_code == 200
    assert "Stopped watching other/lib." in resp.text and str(mirror_dir) in resp.text
    assert 'hx-swap-oob="innerHTML"' in resp.text
    assert "HX-Reswap" not in resp.headers
    assert db.get_repo(repo["id"]) is None
    assert (mirror_dir / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main\n"
    assert "other/lib" not in c.get("/repos?tab=all").text


def test_stop_watching_refuses_an_own_repository(authed: Authed) -> None:
    c, token = authed
    db.upsert_repo(repo_row("me/own"))
    repo = db.get_repo_by_name("me/own")
    assert "Stop watching" not in c.get("/repos?tab=all").text

    resp = c.post(f"/watch/repos/{repo['id']}/stop", headers={**HX, "X-CSRF-Token": token})
    assert resp.headers["HX-Reswap"] == "none"  # HTMX must keep the table row
    assert "stays in the list" in resp.text
    assert db.get_repo(repo["id"]) is not None


def test_stop_watching_warns_when_the_owner_is_watched(authed: Authed) -> None:
    c, _token = authed
    db.upsert_repo(repo_row("octocat/a", source="watched"))
    assert "is a watched owner" not in c.get("/repos?tab=watched").text
    db.add_watch_owner("Octocat", "user", False, False)
    assert "octocat is a watched owner. The next scan job adds this repository again." in (
        c.get("/repos?tab=watched").text)


def test_owner_routes_add_and_remove(authed: Authed, gh: GH) -> None:
    c, token = authed
    headers = {**HX, "X-CSRF-Token": token}
    gh(lambda r: httpx.Response(200, json={"login": "octocat", "type": "User"}))

    resp = c.post("/watch/owners", data={"login": "octocat", "include_forks": "1"},
                  headers=headers)
    assert resp.status_code == 200
    assert 'id="watch-owners-list"' in resp.text and "with forks" in resp.text
    assert "with archived" not in resp.text
    assert "Added octocat. The next scan job adds the public repositories of octocat." in resp.text
    owner = db.list_watch_owners()[0]
    assert "octocat" in c.get("/repos?tab=watched").text

    db.upsert_repo(repo_row("octocat/a", source="watched"))
    resp = c.post(f"/watch/owners/{owner['id']}/remove", headers=headers)
    assert resp.status_code == 200
    assert "The repositories that are in the list stay there." in resp.text
    assert "No watched owners." in resp.text
    assert db.list_watch_owners() == []
    assert db.get_repo_by_name("octocat/a") is not None  # the repository stays
