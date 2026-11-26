"""Runtime values that jobs need: the GitHub token and the storage roots.

All code reads them through these functions, never from `get_settings()`
directly, so that the place they are stored can change in one file.

The values set in the UI (settings table) win. The env values are the fallback.
"""
from pathlib import Path

from . import db, secrets_store
from .config import get_settings

# Settings-table keys.
TOKEN_KEY = "github_token_enc"       # Fernet text, never the plain token
MIRROR_KEY = "mirror_root"
PROTECTION_KEY = "protection_root"


def github_token() -> str:
    """Return the GitHub token, or an empty string when none is configured."""
    stored = db.get_setting(TOKEN_KEY) or ""
    if stored:
        try:
            return secrets_store.decrypt(stored)
        except secrets_store.SecretUnreadable:
            # SECRET_KEY changed: the stored token counts as not set. The UI
            # shows this state (see `stored_token_unreadable`).
            pass
    return get_settings().github_token


def mirror_root() -> Path:
    """Folder that holds the bare mirrors (`<owner>/<repo>.git`)."""
    return Path(db.get_setting(MIRROR_KEY) or get_settings().mirror_root)


def protection_root() -> Path:
    """Folder that receives the protection copy of the mirror root."""
    return Path(db.get_setting(PROTECTION_KEY) or get_settings().protection_root)


def token_source() -> str:
    """Say where the active token comes from: 'stored', 'env' or 'none'."""
    stored = db.get_setting(TOKEN_KEY) or ""
    if stored and not stored_token_unreadable():
        return "stored"
    return "env" if get_settings().github_token else "none"


def stored_token_unreadable() -> bool:
    """True when a token is stored but SECRET_KEY can no longer decrypt it."""
    stored = db.get_setting(TOKEN_KEY) or ""
    if not stored:
        return False
    try:
        secrets_store.decrypt(stored)
    except secrets_store.SecretUnreadable:
        return True
    return False


def store_github_token(token: str) -> None:
    """Encrypt the token and store it in the settings table."""
    db.set_setting(TOKEN_KEY, secrets_store.encrypt(token))


def remove_stored_github_token() -> None:
    """Remove the stored token. The env token, if any, becomes active again."""
    db.set_setting(TOKEN_KEY, "")
