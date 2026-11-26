"""Per-repository switch for the metadata capture."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import db
from ..web import render

router = APIRouter()


@router.post("/repos/{repo_id}/metadata-toggle")
def repo_metadata_toggle(request: Request, repo_id: int):
    if not db.get_repo(repo_id):
        raise HTTPException(status_code=404, detail="Repository not found.")
    db.toggle_repo_metadata(repo_id)
    if request.headers.get("HX-Request"):
        return render(request, "_repo_meta_cell.html", {"r": db.get_repo(repo_id)})
    return RedirectResponse("/repos", status_code=303)
