"""Sign-in and sign-out. Public: no session is required to reach these."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth
from ..web import render

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request, username: str = Form(""), password: str = Form("")):
    key = auth.client_key(request)
    if auth.locked_out(key):
        return render(
            request, "login.html",
            {"error": "Too many wrong attempts. Wait a few minutes, then try again."},
            status_code=429,
        )
    if auth.verify(username, password):
        auth.clear_failures(key)
        # Drop the pre-login session so its CSRF token is not carried over.
        request.session.clear()
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)
    auth.record_failure(key)
    return render(request, "login.html", {"error": "Wrong username or password."},
                  status_code=401)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
