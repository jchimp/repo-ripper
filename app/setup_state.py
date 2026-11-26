"""Setup state that more than one page needs.

- Is the first-run setup complete?
- The result of the last token check (stored in the settings table, with no
  secret in it), and the warnings that follow from it.

Route modules and the scan job import this module; it imports no route module.
"""
import json
import logging
import time
from dataclasses import asdict

from . import db, fsbrowse, github, runtime

log = logging.getLogger("repo-ripper")

SETUP_COMPLETE_KEY = "setup_complete"
TOKEN_CHECK_KEY = "token_check"
PROTECTION_SKIP_KEY = "protection_skip"
PROTECTION_SCHEDULE_KEY = "schedule_protection"
# Same value as the protection JobSpec in jobs.py (jobs.py imports this module,
# so it cannot be read from there).
PROTECTION_DEFAULT_CRON = "0 5 * * 0"
EXPIRY_WARN_DAYS = 14

MSG_UNREADABLE = (
    "The stored token cannot be read because SECRET_KEY changed. Enter the token again."
)


def is_complete() -> bool:
    """True when the first-run wizard is done, or an older install is in use.

    An install from before the wizard has a token (from any source) and at
    least one repository row; it must not be sent through the wizard.
    """
    if db.get_setting(SETUP_COMPLETE_KEY) == "1":
        return True
    return bool(runtime.github_token()) and db.count_repos() > 0


def mark_complete() -> None:
    db.set_setting(SETUP_COMPLETE_KEY, "1")


# --- last token check -------------------------------------------------------

def save_token_check(check: github.TokenCheck) -> None:
    """Store the result of a token check. The result holds no secret."""
    db.set_setting(TOKEN_CHECK_KEY, json.dumps(asdict(check)))


def clear_token_check() -> None:
    db.set_setting(TOKEN_CHECK_KEY, "")


def load_token_check() -> dict | None:
    """Return the stored result of the last token check, or None."""
    raw = db.get_setting(TOKEN_CHECK_KEY) or ""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def refresh_token_check() -> None:
    """Check the active token again and store the result. Used by the scan job.

    A network failure keeps the older result: "cannot reach GitHub" says
    nothing about the token, and the scan job reports that failure itself.
    """
    try:
        token = runtime.github_token()
        if not token:
            return
        check = github.check_token(token)
        if check.ok or check.error_kind == "rejected":
            save_token_check(check)
    except Exception:
        # This refresh is a side task of the scan job. A fault here must not
        # stop the scan, so it is logged and the older result stays.
        log.exception("Token check during scan failed")


def days_left(expires_at: float | None, now: float | None = None) -> int | None:
    """Whole days until the token expires. Negative when it is expired."""
    if expires_at is None:
        return None
    return int((expires_at - (now or time.time())) // 86400)


def token_warnings(check: dict | None, now: float | None = None) -> list[str]:
    """Warnings for a successful check: near expiry, classic token without `repo`."""
    if not check or not check.get("ok"):
        return []
    out: list[str] = []
    left = days_left(check.get("expires_at"), now)
    if left is not None and left < 0:
        out.append("The token is expired. Make a new token on GitHub and replace it here.")
    elif left is not None and left < EXPIRY_WARN_DAYS:
        out.append(
            f"The token expires in {left} day{'s' if left != 1 else ''}. "
            "Make a new token on GitHub and replace it here before that date."
        )
    scopes = check.get("scopes")
    if check.get("token_type") == "classic" and "repo" not in (scopes or []):
        out.append(
            "This classic token does not have the \"repo\" scope. "
            "Private repositories will not be visible."
        )
    return out


def token_view() -> dict:
    """Everything the UI shows about the token. Never includes the token."""
    source = runtime.token_source()
    check = load_token_check() if source != "none" else None
    return {
        "source": source,                                   # stored | env | none
        "unreadable": runtime.stored_token_unreadable(),
        "check": check,
        "warnings": token_warnings(check),
        "unreadable_message": MSG_UNREADABLE,
    }


def token_health() -> dict | None:
    """Return a dashboard notice, or None when there is nothing to act on."""
    view = token_view()
    if view["unreadable"]:
        return {"level": "fail", "message": MSG_UNREADABLE}
    if view["source"] == "none":
        # Not a failure: watched public repositories need no token.
        return {"level": "warn",
                "message": "No GitHub token is set. The app cannot find your own repositories "
                           "or mirror private ones. Watched public repositories still work."}
    check = view["check"]
    if check and not check.get("ok") and check.get("error_kind") == "rejected":
        return {"level": "fail",
                "message": "GitHub rejected the token at the last check. "
                           "The token is wrong or expired. Replace the token."}
    left = days_left(check.get("expires_at")) if check and check.get("ok") else None
    if left is not None and left < 0:
        return {"level": "fail",
                "message": "The GitHub token is expired. Replace the token."}
    if left is not None and left < EXPIRY_WARN_DAYS:
        return {"level": "warn",
                "message": f"The GitHub token expires in {left} day{'s' if left != 1 else ''}. "
                           "Replace the token before that date."}
    return None


# --- storage ----------------------------------------------------------------

def protection_skipped() -> bool:
    return db.get_bool(PROTECTION_SKIP_KEY, False)


def has_synced_mirrors() -> bool:
    """True when at least one repository was mirrored to the current folder."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM repos WHERE last_sync_at IS NOT NULL LIMIT 1"
        ).fetchone()
    return row is not None


def save_token(raw: str) -> github.TokenCheck:
    """Check a new token with GitHub and store it when GitHub accepts it.

    The token is stored encrypted. A token that GitHub rejects, or that cannot
    be checked (no network), is not stored.

    Returns:
        The check result. `ok` says if the token was stored.
    """
    token = (raw or "").strip()
    check = github.check_token(token)
    if check.ok:
        runtime.store_github_token(token)
        save_token_check(check)
    return check


def save_storage(mirror_raw: str, protection_raw: str,
                 skip_protection: bool | None) -> dict[str, str]:
    """Validate and store the two storage folders.

    Data is never moved: a new mirror folder gets new clones at the next sync.
    The caller must call `scheduler.configure_jobs()` after a save, because
    the protection schedule can change.

    Args:
        mirror_raw: Mirror folder as typed or picked.
        protection_raw: Protection folder as typed or picked.
        skip_protection: True = no protection copy (schedule set to empty,
            folder not changed). False = use the folder. None = store the
            folder and leave the skip state and the schedule as they are.

    Returns:
        Error messages by field name. Empty when the values were stored.
    """
    errors: dict[str, str] = {}
    mirror = protection = None
    try:
        mirror, _ = fsbrowse.prepare_folder(mirror_raw)
    except fsbrowse.BrowseError as exc:
        errors[runtime.MIRROR_KEY] = str(exc)
    if not skip_protection:
        try:
            protection, _ = fsbrowse.prepare_folder(protection_raw)
        except fsbrowse.BrowseError as exc:
            errors[runtime.PROTECTION_KEY] = str(exc)
    if errors:
        return errors

    db.set_setting(runtime.MIRROR_KEY, str(mirror))
    if protection is not None:
        db.set_setting(runtime.PROTECTION_KEY, str(protection))
    if skip_protection is True:
        db.set_setting(PROTECTION_SKIP_KEY, "1")
        db.set_setting(PROTECTION_SCHEDULE_KEY, "")
    elif skip_protection is False:
        if protection_skipped() and not (db.get_setting(PROTECTION_SCHEDULE_KEY) or "").strip():
            # The schedule was emptied by an earlier "skip": give it back.
            db.set_setting(PROTECTION_SCHEDULE_KEY, PROTECTION_DEFAULT_CRON)
        db.set_setting(PROTECTION_SKIP_KEY, "0")
    return errors


def settings_context() -> dict:
    """Template values for the Storage and GitHub token sections in Settings."""
    return {
        "token": token_view(),
        "protection_skipped": protection_skipped(),
        "has_mirrors": has_synced_mirrors(),
    }
