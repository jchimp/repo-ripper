"""Telegram notifications.

Config is read from the DB settings (editable in the UI), falling back to the
env seed. Sending is best-effort: a notification failure never fails a job.
"""
import html

import httpx

from . import db
from .config import get_settings


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
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception:
        pass  # never let notification errors break a job


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
    except Exception:
        return False
