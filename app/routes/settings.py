"""Settings page: one tab, one URL and one form for each section."""
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, fsbrowse, jobs, notify, runtime, scheduler, setup_state
from ..config import get_settings
from ..metadata import assets_job
from ..web import render

router = APIRouter()


@dataclass(frozen=True)
class Tab:
    """One section of the Settings page and the keys its form saves.

    A save writes only these keys. A checkbox that is not in the form is "off",
    so a save must never touch the checkboxes of the other tabs.
    """

    slug: str
    title: str
    bools: tuple[str, ...] = ()
    texts: tuple[str, ...] = ()
    numbers: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    schedules: bool = False


TABS: tuple[Tab, ...] = (
    # Folders and token have their own forms (POST /settings/storage, /settings/token).
    Tab("storage", "Storage and token"),
    Tab("discovery", "Discovery",
        bools=("include_forks", "include_archived", "include_wikis", "fetch_lfs",
               "auto_enable_new"),
        numbers=("pull_workers",)),
    Tab("metadata", "Metadata",
        bools=("metadata_enabled",),
        numbers=("assets_keep_releases", "assets_max_mb")),
    Tab("schedules", "Schedules", schedules=True),
    Tab("protection", "Protection copy", bools=("protection_delete",)),
    Tab("notifications", "Notifications",
        bools=("telegram_enabled", "notify_on_success", "notify_on_fail"),
        texts=("telegram_chat_id",),
        # Secrets are never sent back to the browser. An empty field on save
        # means "keep the stored value".
        secrets=("telegram_bot_token",)),
)
_TABS = {tab.slug: tab for tab in TABS}

_SECRET_KEYS = tuple(key for tab in TABS for key in tab.secrets)
# Same idea for values that have their own forms: the encrypted GitHub token
# and the last token check never go into the page context as-is.
_HIDDEN_KEYS = (runtime.TOKEN_KEY, setup_state.TOKEN_CHECK_KEY)

# Whole numbers, limited to (low, high). A value that is not a number keeps the stored one.
_NUMBER_LIMITS = {
    "pull_workers": (1, 8),
    "assets_keep_releases": (0, 100000),
    "assets_max_mb": (1, assets_job.MAX_MB_LIMIT),
}

_NOTICES = {
    "1": "Settings saved.",
    "schedules": "Schedules saved and applied.",
    "storage": "Folders saved.",
    "token": "Token checked and stored.",
    "token-removed": "Stored token removed.",
}


def _tab(section: str) -> Tab:
    tab = _TABS.get(section)
    if tab is None:
        raise HTTPException(status_code=404)
    return tab


def _page(request: Request, tab: Tab, extra: dict | None = None, status_code: int = 200,
          typed: dict[str, str] | None = None) -> HTMLResponse:
    """Render one tab of the Settings page.

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
        "tabs": TABS,
        "tab": tab,
        "s": s,
        "secrets_set": secrets_set,
        "job_specs": list(jobs.JOBS.values()),
        "pull_workers": jobs.pull_workers(),
        "assets_keep_releases": assets_job.keep_releases(),
        "assets_max_mb": assets_job.max_mb(),
        "cfg": get_settings(),
        "mirror_root": mirror,
        "protection_root": protection,
        **ctx,
    }, status_code=status_code)


@router.get("/settings")
def settings_index() -> RedirectResponse:
    return RedirectResponse(f"/settings/{TABS[0].slug}", status_code=303)


@router.get("/settings/{section}", response_class=HTMLResponse)
def settings_tab(request: Request, section: str):
    return _page(request, _tab(section))


def _save(request: Request, tab: Tab,
          form: Mapping[str, object]) -> HTMLResponse | RedirectResponse:
    """Check the form of one tab, then store only its keys. Blocking: runs in the thread pool."""
    values: dict[str, str] = {key: "1" if form.get(key) else "0" for key in tab.bools}
    for key in tab.texts:
        if key in form:
            values[key] = str(form.get(key)).strip()

    if tab.schedules:
        schedule_keys = tuple(spec.schedule_key for spec in jobs.JOBS.values())
        for key in schedule_keys:
            if key in form:
                values[key] = str(form.get(key)).strip()
        # Check all schedules before the first write. A bad string in the database
        # is a job without a schedule after the next start.
        cron_errors = {key: problem for key in schedule_keys
                       if (problem := scheduler.cron_problem(values.get(key, "")))}
        if cron_errors:
            # Show the tab again with what the user typed.
            return _page(request, tab, {"cron_errors": cron_errors},
                         status_code=400, typed=values)

    for key in tab.numbers:
        try:
            number = int(str(form.get(key) or "").strip())
        except ValueError:
            continue
        low, high = _NUMBER_LIMITS[key]
        values[key] = str(max(low, min(high, number)))
    for key in tab.secrets:
        value = str(form.get(key) or "").strip()
        if value:
            values[key] = value
        elif form.get(f"{key}_clear"):
            values[key] = ""
    db.set_settings(values)  # one transaction
    if tab.schedules:
        scheduler.configure_jobs()  # re-apply cron changes live
        return RedirectResponse("/settings/schedules?saved=schedules", status_code=303)
    return RedirectResponse(f"/settings/{tab.slug}?saved=1", status_code=303)


# --- fixed POST routes: registered before POST /settings/{section} ------------

@router.post("/settings/test-telegram")
def test_telegram():
    ok = notify.test_message()
    msg = "Sent — check Telegram." if ok else "Not sent. Check enable + token + chat ID."
    return HTMLResponse(f'<span class="hint">{msg}</span>')


@router.post("/settings/storage", response_class=HTMLResponse)
def storage_save(request: Request, mirror_root: str = Form(""),
                 protection_root: str = Form("")):
    old_mirror = runtime.mirror_root()
    errors = setup_state.save_storage(mirror_root, protection_root, None)
    if errors:
        return _page(request, _TABS["storage"], {
            "mirror": fsbrowse.folder_field(
                "mirror_root", mirror_root, error=errors.get("mirror_root", "")),
            "protection": fsbrowse.folder_field(
                "protection_root", protection_root, error=errors.get("protection_root", "")),
        }, status_code=400)
    scheduler.configure_jobs()
    # Data is not moved. Tell the owner when synced mirrors stay behind.
    moved = runtime.mirror_root() != old_mirror and setup_state.has_synced_mirrors()
    return RedirectResponse(
        f"/settings/storage?saved=storage{'&moved=1' if moved else ''}#storage",
        status_code=303)


@router.post("/settings/token", response_class=HTMLResponse)
def token_save(request: Request, token: str = Form("")):
    check = setup_state.save_token(token)
    if not check.ok:
        return _page(request, _TABS["storage"],
                     {"token_error": f"{check.error} The token was not stored."},
                     status_code=400)
    return RedirectResponse("/settings/storage?saved=token#github-token", status_code=303)


@router.post("/settings/token/remove")
def token_remove(confirm: str = Form("")):
    # The page asks "are you sure" in the browser; the hidden field makes sure
    # that only that form, not a stray POST, removes the token.
    if confirm == "yes":
        runtime.remove_stored_github_token()
        setup_state.clear_token_check()
        return RedirectResponse(
            "/settings/storage?saved=token-removed#github-token", status_code=303)
    return RedirectResponse("/settings/storage#github-token", status_code=303)


@router.post("/settings/{section}")
async def settings_save(request: Request, section: str):
    # "storage" never gets here: its fixed route above takes the POST first.
    tab = _tab(section)
    form = await request.form()
    # SQLite writes and the scheduler block: keep them off the event loop.
    return await run_in_threadpool(_save, request, tab, form)
