"""Shared web helpers: templates, rendering, CSRF.

Route modules import from here, so that they do not import each other.
"""
import secrets
import time
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _fmt_size(n: int | None) -> str:
    if not n:
        return "—"
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024 or unit == "TB":
            return f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} TB"


templates.env.filters["fmt_time"] = _fmt_time
templates.env.filters["fmt_size"] = _fmt_size


def csrf_token(request: Request) -> str:
    """Return the session's CSRF token, creating it on first use."""
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def csrf_protect(request: Request) -> None:
    """App-wide dependency: every state-changing request must carry the token.

    HTMX sends it in the `X-CSRF-Token` header (set once on <body>). Plain
    forms send it in a hidden `csrf_token` field.
    """
    if request.method in _SAFE_METHODS:
        return
    expected = request.session.get("csrf")
    sent = request.headers.get("X-CSRF-Token")
    if sent is None:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            # Starlette caches the parsed form, so route handlers can read it again.
            sent = (await request.form()).get("csrf_token")
    if not expected or not isinstance(sent, str) or not secrets.compare_digest(sent, expected):
        raise HTTPException(status_code=403, detail="CSRF token missing or wrong. Reload the page.")


def render(request: Request, name: str, ctx: dict | None = None,
           status_code: int = 200) -> HTMLResponse:
    """Render a template with the values every page needs."""
    ctx = dict(ctx or {})
    ctx.setdefault("user", request.session.get("user"))
    ctx["csrf_token"] = csrf_token(request)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
