"""Repository inventory: list with filters, bulk actions, detail page, sync now."""
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import db, jobs, mirror, progress, runtime, scheduler, viewhelpers
from ..web import render

router = APIRouter()

_FILTER_KEYS = ("q", "status", "visibility", "source", "backup", "flag", "sort")


def _filters(values: dict[str, str]) -> dict[str, str]:
    """Take the filter values out of a query string or a form."""
    f = {key: str(values.get(key) or "").strip() for key in _FILTER_KEYS}
    if f["sort"] not in db.REPO_SORTS:
        f["sort"] = "name"
    return f


def _list_ctx(f: dict[str, str], message: str = "", message_kind: str = "info") -> dict:
    repos = db.query_repos(**f, running=progress.running_names())
    # Only the values that differ from the default, so that the URL is short.
    query = urlencode({k: v for k, v in f.items() if v and (k, v) != ("sort", "name")})
    return {
        "repos": repos,
        "total": db.count_repos(),
        "f": f,
        "query": query,
        "filtered": any(v for k, v in f.items() if k != "sort"),
        # The list polls only while a job runs.
        "polling": bool(db.running_jobs()),
        "message": message,
        "message_kind": message_kind,
    }


def _start_sync(repo_ids: list[int]) -> str:
    """Start a background sync of these repositories. Returns '' or a refusal."""
    try:
        jid, claimed = jobs.prepare_selected_sync(repo_ids)
    except jobs.SyncRefused as exc:
        return str(exc)
    try:
        scheduler.run_in_background(jobs.run_selected_sync, jid, claimed)
    except Exception as exc:
        # For example the scheduler is stopped. Do not leave the run open.
        jobs.abort_selected_sync(jid, claimed, repr(exc))
        return "The sync did not start. Read the job log on the dashboard."
    return ""


@router.get("/repos", response_class=HTMLResponse)
def repos_page(request: Request):
    ctx = _list_ctx(_filters(dict(request.query_params)),
                    request.session.pop("flash", ""))
    if request.headers.get("HX-Target") == "repo-list":
        return render(request, "_repo_list.html", ctx)
    return render(request, "repos.html", ctx)


@router.post("/repos/bulk", response_class=HTMLResponse)
def repos_bulk(
    request: Request,
    ids: Annotated[list[int] | None, Form()] = None,
    action: str = Form(""),
    q: str = Form(""), status: str = Form(""), visibility: str = Form(""),
    source: str = Form(""), backup: str = Form(""), flag: str = Form(""),
    sort: str = Form(""),
):
    """Apply one action to the checked rows: 'enable', 'disable' or 'sync'."""
    ids = sorted(set(ids or []))
    message, kind = "", "info"
    if not ids:
        message, kind = "Select one repository or more first.", "warn"
    elif action in ("enable", "disable"):
        count = db.set_repos_enabled(ids, action == "enable")
        message = f"Backup turned {'on' if action == 'enable' else 'off'} for {count} repositories."
    elif action == "sync":
        refusal = _start_sync(ids)
        if refusal:
            message, kind = refusal, "warn"
        else:
            message = f"Sync started for {len(ids)} repositories."
    else:
        message, kind = "Unknown action.", "warn"

    if request.headers.get("HX-Request"):
        f = _filters({"q": q, "status": status, "visibility": visibility, "source": source,
                      "backup": backup, "flag": flag, "sort": sort})
        return render(request, "_repo_list.html", _list_ctx(f, message, kind))
    request.session["flash"] = message
    return RedirectResponse("/repos", status_code=303)


@router.post("/repos/{repo_id}/toggle")
def repo_toggle(request: Request, repo_id: int, back: str = Form("")):
    db.toggle_repo(repo_id)
    if not db.get_repo(repo_id):
        return RedirectResponse("/repos", status_code=303)
    if request.headers.get("HX-Request"):
        return render(request, "_repo_row.html", {"r": db.get_repo(repo_id)})
    target = f"/repos/{repo_id}" if back == "detail" else "/repos"
    return RedirectResponse(target, status_code=303)


@router.post("/repos/{repo_id}/sync")
def repo_sync(request: Request, repo_id: int, back: str = Form("")):
    """Mirror one repository now, in the background."""
    repo = db.get_repo(repo_id)
    if not repo:
        return RedirectResponse("/repos", status_code=303)
    refusal = _start_sync([repo_id])
    if request.headers.get("HX-Request"):
        resp = render(request, "_repo_row.html", {"r": repo, "row_note": refusal})
        if not refusal:
            # The list listens for this event and starts to poll.
            resp.headers["HX-Trigger"] = "repo-sync-started"
        return resp
    if refusal:
        request.session["flash"] = refusal
    target = f"/repos/{repo_id}" if back == "detail" else "/repos"
    return RedirectResponse(target, status_code=303)


@router.get("/repos/{repo_id}", response_class=HTMLResponse)
def repo_detail(request: Request, repo_id: int):
    repo = db.get_repo(repo_id)
    if not repo:
        return RedirectResponse("/repos", status_code=303)
    path = mirror.mirror_dir(repo["full_name"])
    return render(request, "repo_detail.html", {
        "r": repo,
        "status": viewhelpers.repo_status(repo),
        "mirror_path": str(path),
        "mirror_exists": (path / "HEAD").is_file(),
        "mirror_root": str(runtime.mirror_root()),
        "log": db.repo_sync_log(repo_id, 50),
        "flash": request.session.pop("flash", ""),
    })


@router.get("/repos/{repo_id}/live", response_class=HTMLResponse)
def repo_live(request: Request, repo_id: int):
    """The polled status block of the detail page while the repository syncs."""
    repo = db.get_repo(repo_id)
    if not repo:
        return Response(status_code=204, headers={"HX-Redirect": "/repos"})
    status = viewhelpers.repo_status(repo)
    resp = render(request, "_repo_live.html", {"r": repo, "status": status})
    if status != "running":
        resp.headers["HX-Refresh"] = "true"   # done: show the new facts and history
    return resp
