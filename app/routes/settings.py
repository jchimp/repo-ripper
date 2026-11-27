"""Settings page."""
from collections.abc import Mapping

from fastapi import APIRouter, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, fsbrowse, jobs, notify, runtime, scheduler, setup_state
from ..config import get_settings
from ..metadata import assets_job
from ..web import render

router = APIRouter()

_BOOL_KEYS = (
    "include_forks", "include_archived", "include_wikis", "fetch_lfs",
    "telegram_enabled", "notify_on_success", "notify_on_fail", "protection_delete",
    "auto_enable_new",
    "metadata_enabled",
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


def _page(request: Request, extra: dict | None = None, status_code: int = 200,
          typed: dict[str, str] | None = None) -> HTMLResponse:
    """Render the Settings page.

    `typed` holds values of a form that was not saved. They replace the stored
    values in the fields, so the user does not type them again.
    """
    s = db.get_all_settings()
    secrets_set = {k: bool(s.pop(k, "")) for k in _SECRET_KEYS}
    for key in _HIDDEN_KEYS:
        s.pop(key, None)
    s.update(typed or {})
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
        "pull_workers": jobs.pull_workers(),
        "assets_keep_releases": assets_job.keep_releases(),
        "assets_max_mb": assets_job.max_mb(),
        "cfg": get_settings(),
        "mirror_root": runtime.mirror_root(),
        "protection_root": runtime.protection_root(),
        "saved": request.query_params.get("saved"),
        **ctx,
    }, status_code=status_code)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return _page(request)


_NUMBER_KEYS = ("pull_workers", "assets_keep_releases", "assets_max_mb")


def _save(request: Request, form: Mapping[str, object]) -> HTMLResponse | RedirectResponse:
    """Check the main form, then store it. Blocking: runs in the thread pool."""
    values: dict[str, str] = {key: "1" if form.get(key) else "0" for key in _BOOL_KEYS}
    schedule_keys = tuple(spec.schedule_key for spec in jobs.JOBS.values())
    for key in _TEXT_KEYS + schedule_keys:
        if key in form:
            values[key] = str(form.get(key)).strip()

    # Check all schedules before the first write. A bad string in the database
    # is a job without a schedule after the next start.
    cron_errors = {}
    for key in schedule_keys:
        problem = scheduler.cron_problem(values.get(key, ""))
        if problem:
            cron_errors[key] = problem
    if cron_errors:
        # Show the form again with what the user typed. Secrets are not sent back.
        typed = dict(values)
        numbers = {key: str(form.get(key)).strip() for key in _NUMBER_KEYS if key in form}
        return _page(request, {"cron_errors": cron_errors, **numbers},
                     status_code=400, typed=typed)

    if "pull_workers" in form:
        try:
            workers = int(str(form.get("pull_workers")).strip())
        except ValueError:
            workers = 0  # not a number: keep the stored value
        if workers:
            values["pull_workers"] = str(max(1, min(8, workers)))
    # Whole numbers with limits. A value that is not a number keeps the stored one.
    for key, low, high in (("assets_keep_releases", 0, 100000),
                           ("assets_max_mb", 1, assets_job.MAX_MB_LIMIT)):
        raw = str(form.get(key) or "").strip()
        if raw.isdigit():
            values[key] = str(max(low, min(high, int(raw))))
    for key in _SECRET_KEYS:
        value = str(form.get(key) or "").strip()
        if value:
            values[key] = value
        elif form.get(f"{key}_clear"):
            values[key] = ""
    db.set_settings(values)  # one transaction
    scheduler.configure_jobs()  # re-apply cron changes live
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    # SQLite writes and the scheduler block: keep them off the event loop.
    return await run_in_threadpool(_save, request, form)


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
