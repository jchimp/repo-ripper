"""Session-based single-admin auth.

The admin password from the env is bcrypt-hashed once at startup; requests
verify against that hash, so the plaintext isn't held for the process lifetime
beyond the initial hash call.
"""
import secrets

import bcrypt
from fastapi import Request

from .config import get_settings

_PW_HASH: bytes | None = None


def init_auth() -> None:
    global _PW_HASH
    pw = get_settings().admin_password.encode()
    _PW_HASH = bcrypt.hashpw(pw, bcrypt.gensalt())


def verify(username: str, password: str) -> bool:
    s = get_settings()
    user_ok = secrets.compare_digest(username, s.admin_username)
    pass_ok = _PW_HASH is not None and bcrypt.checkpw(password.encode(), _PW_HASH)
    # Evaluate both regardless to avoid short-circuit timing leaks.
    return user_ok and pass_ok


class NotAuthenticated(Exception):
    """Raised by the require_user dependency when no session is present."""


def require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise NotAuthenticated()
    return user
