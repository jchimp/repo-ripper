"""Watch public repositories: add one, watch an owner, stop watching.

The panel and the row parts get their data from the template globals at the
end of this file, so that the Repositories route does not need to know them.
"""
from pathlib import Path
from sqlite3 import Row
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import db, runtime, watch
from ..web import render, templates

router = APIRouter()


def _mirror_path(repo: Row) -> Path:
    return runtime.mirror_root() / (repo["mirror_path"] or f"{repo['full_name']}.git")


def _owners_fragment(request: Request, ok: bool, message: str) -> HTMLResponse:
    return render(request, "_watch_owners_update.html", {
        "owners": db.list_watch_owners(), "ok": ok, "message": message,
    })


@router.post("/watch/repos")
def watch_repo_add(request: Request, repo: str = Form("")):
    result = watch.add_repo(repo)
    if not result.ok:
        return render(request, "_watch_result.html", {"ok": False, "message": result.message})
    # Reload the page: the new row must show in the table, which this module
    # does not own. The panel shows the result message after the reload.
    target = "/repos?" + urlencode({"tab": "watched", "watched": result.name})
    if request.headers.get("HX-Request"):
        resp = Response(status_code=204)
        resp.headers["HX-Redirect"] = target
        return resp
    return RedirectResponse(target, status_code=303)


@router.post("/watch/repos/{repo_id}/stop")
def watch_repo_stop(request: Request, repo_id: int):
    """Remove a watched repository from the list. The mirror stays on disk."""
    repo = db.get_repo(repo_id)
    if repo is None:
        ok, message = False, "This repository is not in the list. Load the page again."
    elif not db.delete_watched_repo(repo_id):
        ok, message = False, (
            f"{repo['full_name']} is one of your own repositories and stays in the list. "
            "Use the backup switch to turn it off."
        )
    else:
        ok, message = True, (
            f"Stopped watching {repo['full_name']}. The mirror files stay in "
            f"{_mirror_path(repo)}. You can delete them by hand."
        )
    if not request.headers.get("HX-Request"):
        return RedirectResponse("/repos?tab=watched", status_code=303)
    resp = render(request, "_watch_result.html", {"ok": ok, "message": message, "oob": True})
    if not ok and repo is not None:
        # The button asks HTMX to delete the table row. Keep the row.
        resp.headers["HX-Reswap"] = "none"
    return resp


@router.post("/watch/owners")
def watch_owner_add(request: Request, login: str = Form(""),
                    include_forks: str | None = Form(None),
                    include_archived: str | None = Form(None)):
    result = watch.add_owner(login, bool(include_forks), bool(include_archived))
    if not request.headers.get("HX-Request"):
        return RedirectResponse("/repos?tab=watched", status_code=303)
    return _owners_fragment(request, result.ok, result.message)


@router.post("/watch/owners/{owner_id}/remove")
def watch_owner_remove(request: Request, owner_id: int):
    owner = db.get_watch_owner(owner_id)
    if owner is None:
        ok, message = False, "This owner is not in the list. Load the page again."
    else:
        db.remove_watch_owner(owner_id)
        ok, message = True, (
            f"Removed {owner['login']}. The scan job does not look for new repositories "
            f"of {owner['login']}. The repositories that are in the list stay there."
        )
    if not request.headers.get("HX-Request"):
        return RedirectResponse("/repos?tab=watched", status_code=303)
    return _owners_fragment(request, ok, message)


# --- template globals --------------------------------------------------------

def _watch_notice(request: Request) -> str | None:
    """Result message after the reload that follows a successful add.

    The name comes from the query string, so it is shown only when it is a
    watched repository in the table: a made-up link cannot show made-up text.
    """
    name = request.query_params.get("watched")
    repo = db.get_repo_by_name(name) if name else None
    if repo is None or repo["source"] != "watched":
        return None
    return f"Added {repo['full_name']}. The next pull job makes the mirror."


def _watch_stop_info(repo: Row) -> dict:
    """Facts for the "Stop watching" confirmation text of one row."""
    owner = repo["full_name"].split("/", 1)[0]
    return {
        "path": _mirror_path(repo),
        "owner": owner,
        "owner_watched": db.get_watch_owner_by_login(owner) is not None,
    }


templates.env.globals.update(
    watch_notice=_watch_notice,
    watch_owners=db.list_watch_owners,
    watch_stop_info=_watch_stop_info,
)
