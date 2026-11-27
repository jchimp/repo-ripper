"""Per-repository switches for the metadata capture and the release files."""
from fastapi import APIRouter, Form, HTTPException, Request
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


@router.post("/repos/{repo_id}/assets-toggle")
def repo_assets_toggle(request: Request, repo_id: int, back: str = Form("")):
    """Switch the download of release files of one repository on or off."""
    if not db.get_repo(repo_id):
        raise HTTPException(status_code=404, detail="Repository not found.")
    db.toggle_repo_assets(repo_id)
    if back == "releases":      # the button on the Releases page of the repository
        return RedirectResponse(f"/repos/{repo_id}/releases", status_code=303)
    if request.headers.get("HX-Request"):
        return render(request, "_repo_meta_cell.html", {"r": db.get_repo(repo_id)})
    return RedirectResponse("/repos", status_code=303)
