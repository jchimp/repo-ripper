"""Settings page."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, jobs, notify, runtime, scheduler
from ..config import get_settings
from ..web import render

router = APIRouter()

_BOOL_KEYS = (
    "include_forks", "include_archived", "include_wikis", "fetch_lfs",
    "telegram_enabled", "notify_on_success", "notify_on_fail", "protection_delete",
)
_TEXT_KEYS = ("telegram_chat_id",)
# Secrets are never sent back to the browser. An empty field on save means
# "keep the stored value".
_SECRET_KEYS = ("telegram_bot_token",)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    s = db.get_all_settings()
    secrets_set = {k: bool(s.pop(k, "")) for k in _SECRET_KEYS}
    return render(request, "settings.html", {
        "s": s,
        "secrets_set": secrets_set,
        "job_specs": list(jobs.JOBS.values()),
        "cfg": get_settings(),
        "mirror_root": runtime.mirror_root(),
        "protection_root": runtime.protection_root(),
        "saved": request.query_params.get("saved"),
    })


@router.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    for key in _BOOL_KEYS:
        db.set_setting(key, "1" if form.get(key) else "0")
    schedule_keys = tuple(spec.schedule_key for spec in jobs.JOBS.values())
    for key in _TEXT_KEYS + schedule_keys:
        if key in form:
            db.set_setting(key, str(form.get(key)).strip())
    for key in _SECRET_KEYS:
        value = str(form.get(key) or "").strip()
        if value:
            db.set_setting(key, value)
        elif form.get(f"{key}_clear"):
            db.set_setting(key, "")
    scheduler.configure_jobs()  # re-apply cron changes live
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/test-telegram")
def test_telegram():
    ok = notify.test_message()
    msg = "Sent — check Telegram." if ok else "Not sent. Check enable + token + chat ID."
    return HTMLResponse(f'<span class="hint">{msg}</span>')
