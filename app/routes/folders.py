"""Folder browser (path picker) partials. Used by the setup wizard and Settings.

Every path from the browser goes through `fsbrowse`, which refuses anything
outside the allowed roots. Errors are shown inside the partial with status 200,
because HTMX does not swap error responses.
"""
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import fsbrowse, runtime
from ..web import render

router = APIRouter()


def _field(name: str) -> str:
    # The name is used in element ids, so only known names are accepted.
    if name not in fsbrowse.FIELDS:
        raise HTTPException(status_code=400, detail="Unknown folder field.")
    return name


def _picker(request: Request, field: str, raw: str, error: str = "") -> HTMLResponse:
    listing = None
    if raw.strip():
        try:
            listing = fsbrowse.list_dir(raw)
        except fsbrowse.BrowseError as exc:
            error = error or str(exc)
            listing = fsbrowse.nearest_listing(raw)
    if listing is None:
        listing = fsbrowse.roots_listing()
    return render(request, "_picker.html", {
        "field": field,
        "label": fsbrowse.FIELDS[field][0],
        "listing": listing,
        "error": error,
    })


@router.get("/browse", response_class=HTMLResponse)
def browse(request: Request, field: str = "", path: str | None = None):
    field = _field(field)
    if path is None:
        # "Browse" sends the current value of the folder field under its own name.
        path = request.query_params.get(field, "")
    return _picker(request, field, path)


@router.get("/browse/close", response_class=HTMLResponse)
def browse_close():
    return HTMLResponse("")


@router.post("/browse/mkdir", response_class=HTMLResponse)
def browse_mkdir(request: Request, field: str = Form(""), path: str = Form(""),
                 name: str = Form("")):
    field = _field(field)
    try:
        made = fsbrowse.make_dir(path, name)
    except fsbrowse.BrowseError as exc:
        return _picker(request, field, path, error=str(exc))
    return _picker(request, field, str(made))


@router.post("/browse/use", response_class=HTMLResponse)
def browse_use(request: Request, field: str = Form(""), path: str = Form(""),
               mirror_root: str = Form(""), protection_root: str = Form("")):
    """Select a folder: return the folder field with space and write-test result."""
    field = _field(field)
    current = {"mirror_root": mirror_root, "protection_root": protection_root}
    try:
        folder = fsbrowse.safe_resolve(path)
    except fsbrowse.BrowseError as exc:
        return render(request, "_folder_field.html", {
            "f": fsbrowse.folder_field(field, current[field], error=str(exc)),
        })
    check = fsbrowse.check_folder(folder)
    current[field] = str(folder)
    # Compare with the other field on the page; in pages that show one field
    # only, compare with the folder in use.
    other = "protection_root" if field == "mirror_root" else "mirror_root"
    other_value = current[other] or str(
        runtime.protection_root() if other == "protection_root" else runtime.mirror_root()
    )
    return render(request, "_folder_field.html", {
        "f": fsbrowse.folder_field(
            field, folder, check=check,
            warning=fsbrowse.overlap_warning(folder, other_value),
        ),
    })
