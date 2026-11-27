"""Telegram notifications.

Config is read from the DB settings (editable in the UI), falling back to the
env seed. Sending is best-effort: a notification failure never fails a job.
"""
import html
import logging

import httpx

from . import db
from .config import get_settings

log = logging.getLogger("repo-ripper")


def _config() -> tuple[str, str] | None:
    s = get_settings()
    if not db.get_bool("telegram_enabled"):
        return None
    token = db.get_setting("telegram_bot_token") or s.telegram_bot_token
    chat = db.get_setting("telegram_chat_id") or s.telegram_chat_id
    if not token or not chat:
        return None
    return token, chat


def send(text: str) -> None:
    cfg = _config()
    if not cfg:
        return
    token, chat = cfg
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as exc:
        # Never let a notification error break a job. Log the type only: the
        # text of an httpx error can hold the URL, and the URL holds the token.
        log.warning("Telegram message not sent: %s", type(exc).__name__)
        return
    if r.status_code != 200:
        log.warning("Telegram message not sent: HTTP status %s", r.status_code)


def notify_job(job_type: str, ok: bool, summary: str) -> None:
    """Respect the per-event success/fail toggles before sending."""
    if ok and not db.get_bool("notify_on_success"):
        return
    if not ok and not db.get_bool("notify_on_fail"):
        return
    icon = "\u2705" if ok else "\u274c"  # check / cross
    body = html.escape(summary)
    send(f"{icon} <b>repo-ripper</b> \u2014 {job_type}\n{body}")


def test_message() -> bool:
    """Send a test ping regardless of the event toggles. Returns True on send."""
    cfg = _config()
    if not cfg:
        return False
    token, chat = cfg
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": "\U0001f9ea repo-ripper test notification"},
            timeout=15,
        )
        return r.status_code == 200
    except Exception as exc:
        # The type only, see send().
        log.warning("Telegram test message not sent: %s", type(exc).__name__)
        return False
