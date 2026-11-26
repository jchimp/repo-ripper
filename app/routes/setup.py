"""First-run setup wizard: storage, token, repositories, start.

Each step stores its values when the user continues, so the wizard can be
left and opened again ("Run setup again" in Settings) with the current values.
"""
import hashlib
import threading
import time
from dataclasses import asdict
from typing import Annotated

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, fsbrowse, github, runtime, scheduler, setup_state
from ..web import render

router = APIRouter()

# The repository list of step 3, kept for a short time so that "Add selected"
# stores the list that the user saw, without a second slow GitHub call. Keyed
# by a hash of the token, so a new token never gets the list of the old one.
_LIST_TTL = 600
_list_cache: dict[str, tuple[float, list[dict]]] = {}
_list_lock = threading.Lock()


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# --- step 1: storage --------------------------------------------------------

def _storage_page(request: Request, mirror: str, protection: str, skip: bool,
                  errors: dict[str, str] | None = None, status_code: int = 200) -> HTMLResponse:
    errors = errors or {}
    warning = "" if skip else fsbrowse.overlap_warning(mirror, protection)
    return render(request, "setup_storage.html", {
        "step": 1,
        "mirror": fsbrowse.folder_field(
            "mirror_root", mirror, error=errors.get("mirror_root", "")),
        "protection": fsbrowse.folder_field(
            "protection_root", protection, error=errors.get("protection_root", ""),
            warning=warning),
        "skip": skip,
        "roots": fsbrowse.allowed_roots(),
        "has_mirrors": setup_state.has_synced_mirrors(),
    }, status_code=status_code)


@router.get("/setup")
def setup_index():
    return _redirect("/setup/storage")


@router.get("/setup/storage", response_class=HTMLResponse)
def storage_page(request: Request):
    return _storage_page(request, str(runtime.mirror_root()), str(runtime.protection_root()),
                         setup_state.protection_skipped())


@router.post("/setup/storage", response_class=HTMLResponse)
def storage_save(request: Request, mirror_root: str = Form(""),
                 protection_root: str = Form(""), skip_protection: str = Form("")):
    skip = bool(skip_protection)
    errors = setup_state.save_storage(mirror_root, protection_root, skip)
    if errors:
        return _storage_page(request, mirror_root, protection_root, skip, errors,
                             status_code=400)
    scheduler.configure_jobs()  # the protection schedule can have changed
    return _redirect("/setup/token")


# --- step 2: token ----------------------------------------------------------

def _token_page(request: Request, error: str = "", status_code: int = 200) -> HTMLResponse:
    return render(request, "setup_token.html", {
        "step": 2,
        "token": setup_state.token_view(),
        "error": error,
    }, status_code=status_code)


@router.get("/setup/token", response_class=HTMLResponse)
def token_page(request: Request):
    return _token_page(request)


@router.post("/setup/token/check", response_class=HTMLResponse)
def token_check(request: Request, token: str = Form("")):
    """Check a pasted token without storing it. The token is not echoed."""
    check = asdict(github.check_token(token))
    return render(request, "_token_result.html", {
        "check": check,
        "warnings": setup_state.token_warnings(check),
    })


@router.post("/setup/token", response_class=HTMLResponse)
def token_save(request: Request, token: str = Form("")):
    if not token.strip():
        if runtime.github_token():
            return _redirect("/setup/repos")  # keep the token that is in use
        return _token_page(request, "Enter a token.", status_code=400)
    check = setup_state.save_token(token)
    if not check.ok:
        return _token_page(request, f"{check.error} The token was not stored.",
                           status_code=400)
    return _redirect("/setup/repos")


# --- step 3: repositories ---------------------------------------------------

def _load_listing(refresh: bool = False) -> tuple[list[dict], str]:
    """Return (repositories, error message). Uses the short-lived cache."""
    token = runtime.github_token()
    if not token:
        return [], "No GitHub token is set. Go back to step 2."
    key = hashlib.sha256(token.encode()).hexdigest()
    with _list_lock:
        cached = _list_cache.get(key)
        if cached and not refresh and time.time() - cached[0] < _LIST_TTL:
            return cached[1], ""
    try:
        repos = github.list_all_repos(include_forks=True, include_archived=True)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return [], f"{github.MSG_REJECTED} Go back to step 2 and enter a new token."
        return [], f"GitHub answered with HTTP {exc.response.status_code}. Try again later."
    except httpx.HTTPError:
        return [], github.MSG_NETWORK
    except RuntimeError as exc:
        return [], f"{exc}."
    with _list_lock:
        _list_cache.clear()
        _list_cache[key] = (time.time(), repos)
    return repos, ""


@router.get("/setup/repos", response_class=HTMLResponse)
def repos_page(request: Request):
    if not runtime.github_token():
        return _redirect("/setup/token")
    return render(request, "setup_repos.html", {"step": 3})


@router.get("/setup/repos/list", response_class=HTMLResponse)
def repos_list(request: Request, refresh: int = 0):
    repos, error = _load_listing(refresh=bool(refresh))
    known = {r["full_name"]: bool(r["enabled"]) for r in db.list_repos()}
    rows = [
        # A repository that is already known keeps its switch; a new one starts selected.
        {**r, "selected": known.get(r["full_name"], True)}
        for r in repos
    ]
    return render(request, "_setup_repo_list.html", {
        "repos": rows,
        "owners": sorted({r["owner"] for r in repos}, key=str.lower),
        "error": error,
    })


@router.post("/setup/repos", response_class=HTMLResponse)
def repos_save(request: Request, repo: Annotated[list[str] | None, Form()] = None):
    repos, error = _load_listing()
    if error:
        return render(request, "setup_repos.html", {"step": 3, "error": error},
                      status_code=502)
    selected = set(repo or [])
    now = time.time()
    for r in repos:
        db.upsert_repo({
            "full_name": r["full_name"],
            "private": r["private"],
            "fork": r["fork"],
            "archived": r["archived"],
            "default_branch": r["default_branch"],
            "mirror_path": f"{r['full_name']}.git",
            "discovered_at": now,
        }, enabled=r["full_name"] in selected, update_enabled=True)
    return _redirect("/setup/start")


# --- step 4: start ----------------------------------------------------------

@router.get("/setup/start", response_class=HTMLResponse)
def start_page(request: Request):
    repos = db.list_repos()
    skip = setup_state.protection_skipped()
    mirror, protection = runtime.mirror_root(), runtime.protection_root()
    return render(request, "setup_start.html", {
        "step": 4,
        "mirror_root": mirror,
        "protection_root": protection,
        "protection_skipped": skip,
        "overlap": "" if skip else fsbrowse.overlap_warning(mirror, protection),
        "token": setup_state.token_view(),
        "repo_count": len(repos),
        "enabled_count": sum(1 for r in repos if r["enabled"]),
    })


@router.post("/setup/start")
def start(request: Request):
    if not runtime.github_token():
        return _redirect("/setup/token")
    setup_state.mark_complete()
    scheduler.trigger_now("pull")
    return _redirect("/")
