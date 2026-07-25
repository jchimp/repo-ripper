"""repo-ripper — FastAPI application entrypoint."""
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, db, notify, scheduler
from .config import get_settings

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# --- template helpers -------------------------------------------------------

def _fmt_time(ts: float | None) -> str:
    if not ts:
        return "\u2014"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _fmt_size(n: int | None) -> str:
    if not n:
        return "\u2014"
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024 or unit == "TB":
            return f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} TB"


templates.env.filters["fmt_time"] = _fmt_time
templates.env.filters["fmt_size"] = _fmt_size


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    auth.init_auth()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="repo-ripper", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=get_settings().secret_key,
    max_age=get_settings().session_max_age,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.exception_handler(auth.NotAuthenticated)
async def _unauth(request: Request, exc: auth.NotAuthenticated):
    if request.headers.get("HX-Request"):
        resp = Response(status_code=401)
        resp.headers["HX-Redirect"] = "/login"
        return resp
    return RedirectResponse("/login", status_code=303)


def render(request: Request, name: str, ctx: dict) -> HTMLResponse:
    ctx["request"] = request
    ctx.setdefault("user", request.session.get("user"))
    return templates.TemplateResponse(name, ctx)


# --- auth routes ------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if auth.verify(username, password):
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", {"error": "Wrong username or password."})


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- dashboard --------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    auth.require_user(request)
    repos = db.list_repos()
    enabled = [r for r in repos if r["enabled"]]
    total_size = sum((r["size_bytes"] or 0) for r in repos)
    ctx = {
        "jobs": db.recent_jobs(15),
        "next_runs": scheduler.next_runs(),
        "repo_count": len(repos),
        "enabled_count": len(enabled),
        "total_size": total_size,
    }
    return render(request, "dashboard.html", ctx)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: int):
    auth.require_user(request)
    job = db.get_job(job_id)
    if not job:
        return RedirectResponse("/", status_code=303)
    return render(request, "job_detail.html", {"job": job})


@app.post("/run/{job_type}")
async def run_now(request: Request, job_type: str):
    auth.require_user(request)
    if job_type in ("scan", "pull", "protection"):
        scheduler.trigger_now(job_type)
    if request.headers.get("HX-Request"):
        resp = Response(status_code=204)
        resp.headers["HX-Redirect"] = "/"
        return resp
    return RedirectResponse("/", status_code=303)


# --- repos ------------------------------------------------------------------

@app.get("/repos", response_class=HTMLResponse)
async def repos_page(request: Request):
    auth.require_user(request)
    return render(request, "repos.html", {"repos": db.list_repos()})


@app.post("/repos/{repo_id}/toggle")
async def repo_toggle(request: Request, repo_id: int):
    auth.require_user(request)
    db.toggle_repo(repo_id)
    if request.headers.get("HX-Request"):
        repo = db.get_repo(repo_id)
        return render(request, "_repo_row.html", {"r": repo})
    return RedirectResponse("/repos", status_code=303)


# --- settings ---------------------------------------------------------------

_BOOL_KEYS = (
    "include_forks", "include_archived", "include_wikis", "fetch_lfs",
    "telegram_enabled", "notify_on_success", "notify_on_fail", "protection_delete",
)
_TEXT_KEYS = (
    "telegram_bot_token", "telegram_chat_id",
    "schedule_scan", "schedule_pull", "schedule_protection",
)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    auth.require_user(request)
    return render(request, "settings.html", {
        "s": db.get_all_settings(),
        "cfg": get_settings(),
        "saved": request.query_params.get("saved"),
    })


@app.post("/settings")
async def settings_save(request: Request):
    auth.require_user(request)
    form = await request.form()
    for key in _BOOL_KEYS:
        db.set_setting(key, "1" if form.get(key) else "0")
    for key in _TEXT_KEYS:
        if key in form:
            db.set_setting(key, str(form.get(key)).strip())
    scheduler.configure_jobs()  # re-apply cron changes live
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/test-telegram")
async def test_telegram(request: Request):
    auth.require_user(request)
    ok = notify.test_message()
    msg = "Sent \u2014 check Telegram." if ok else "Not sent. Check enable + token + chat ID."
    return HTMLResponse(f'<span class="hint">{msg}</span>')
