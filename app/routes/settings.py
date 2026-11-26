"""Settings page."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, fsbrowse, jobs, notify, runtime, scheduler, setup_state
from ..config import get_settings
from ..web import render

router = APIRouter()

_BOOL_KEYS = (
    "include_forks", "include_archived", "include_wikis", "fetch_lfs",
    "telegram_enabled", "notify_on_success", "notify_on_fail", "protection_delete",
    "auto_enable_new",
)
_TEXT_KEYS = ("telegram_chat_id",)
# Secrets are never sent back to the browser. An empty field on save means
# "keep the stored value".
_SECRET_KEYS = ("telegram_bot_token",)
# Same idea for values that have their own forms on this page: the encrypted
# GitHub token and the last token check never go into the page context as-is.
_HIDDEN_KEYS = (runtime.TOKEN_KEY, setup_state.TOKEN_CHECK_KEY)

_NOTICES = {
    "1": "Settings saved. Schedules re-applied.",
    "storage": "Folders saved.",
    "token": "Token checked and stored.",
    "token-removed": "Stored token removed.",
}


def _page(request: Request, extra: dict | None = None, status_code: int = 200) -> HTMLResponse:
    s = db.get_all_settings()
    secrets_set = {k: bool(s.pop(k, "")) for k in _SECRET_KEYS}
    for key in _HIDDEN_KEYS:
        s.pop(key, None)
    mirror, protection = runtime.mirror_root(), runtime.protection_root()
    skipped = setup_state.protection_skipped()
    ctx = {
        **setup_state.settings_context(),
        "mirror": fsbrowse.folder_field("mirror_root", mirror),
        "protection": fsbrowse.folder_field(
            "protection_root", protection,
            warning="" if skipped else fsbrowse.overlap_warning(mirror, protection)),
        "notice": _NOTICES.get(request.query_params.get("saved") or ""),
        "mirror_moved": request.query_params.get("moved"),
    }
    ctx.update(extra or {})
    return render(request, "settings.html", {
        "s": s,
        "secrets_set": secrets_set,
        "job_specs": list(jobs.JOBS.values()),
        "cfg": get_settings(),
        "mirror_root": runtime.mirror_root(),
        "protection_root": runtime.protection_root(),
        "saved": request.query_params.get("saved"),
        **ctx,
    }, status_code=status_code)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return _page(request)


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


# --- storage and GitHub token (own forms on the Settings page) ---------------

@router.post("/settings/storage", response_class=HTMLResponse)
def storage_save(request: Request, mirror_root: str = Form(""),
                 protection_root: str = Form("")):
    old_mirror = runtime.mirror_root()
    errors = setup_state.save_storage(mirror_root, protection_root, None)
    if errors:
        return _page(request, {
            "mirror": fsbrowse.folder_field(
                "mirror_root", mirror_root, error=errors.get("mirror_root", "")),
            "protection": fsbrowse.folder_field(
                "protection_root", protection_root, error=errors.get("protection_root", "")),
        }, status_code=400)
    scheduler.configure_jobs()
    # Data is not moved. Tell the owner when synced mirrors stay behind.
    moved = runtime.mirror_root() != old_mirror and setup_state.has_synced_mirrors()
    return RedirectResponse(
        f"/settings?saved=storage{'&moved=1' if moved else ''}#storage", status_code=303)


@router.post("/settings/token", response_class=HTMLResponse)
def token_save(request: Request, token: str = Form("")):
    check = setup_state.save_token(token)
    if not check.ok:
        return _page(request, {"token_error": f"{check.error} The token was not stored."},
                     status_code=400)
    return RedirectResponse("/settings?saved=token#github-token", status_code=303)


@router.post("/settings/token/remove")
def token_remove(confirm: str = Form("")):
    # The page asks "are you sure" in the browser; the hidden field makes sure
    # that only that form, not a stray POST, removes the token.
    if confirm == "yes":
        runtime.remove_stored_github_token()
        setup_state.clear_token_check()
        return RedirectResponse("/settings?saved=token-removed#github-token", status_code=303)
    return RedirectResponse("/settings#github-token", status_code=303)
