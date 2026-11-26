"""Repository inventory."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db
from ..web import render

router = APIRouter()


@router.get("/repos", response_class=HTMLResponse)
def repos_page(request: Request):
    return render(request, "repos.html", {"repos": db.list_repos()})


@router.post("/repos/{repo_id}/toggle")
def repo_toggle(request: Request, repo_id: int):
    db.toggle_repo(repo_id)
    if request.headers.get("HX-Request"):
        return render(request, "_repo_row.html", {"r": db.get_repo(repo_id)})
    return RedirectResponse("/repos", status_code=303)
