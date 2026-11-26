"""Session-based single-admin auth.

The admin password from the env is bcrypt-hashed once at startup; requests
verify against that hash, so the plaintext isn't held for the process lifetime
beyond the initial hash call.
"""
import secrets
import threading
import time
from collections import defaultdict, deque

import bcrypt
from fastapi import Request

from .config import get_settings

_PW_HASH: bytes | None = None

# Login throttle: after MAX_FAILS wrong attempts inside WINDOW seconds, one
# client address is refused until old attempts leave the window. Kept in
# memory: a restart clears it, which is acceptable for a single-admin LAN app.
MAX_FAILS = 5
WINDOW = 300
_FAILS: dict[str, deque[float]] = defaultdict(deque)
_FAILS_LOCK = threading.Lock()


def init_auth() -> None:
    global _PW_HASH
    pw = get_settings().admin_password.encode()
    _PW_HASH = bcrypt.hashpw(pw, bcrypt.gensalt())


def verify(username: str, password: str) -> bool:
    s = get_settings()
    user_ok = secrets.compare_digest(username.encode(), s.admin_username.encode())
    pass_ok = _PW_HASH is not None and bcrypt.checkpw(password.encode(), _PW_HASH)
    # Evaluate both regardless to avoid short-circuit timing leaks.
    return user_ok and pass_ok


def client_key(request: Request) -> str:
    """Address used for throttling. Behind a reverse proxy this is the proxy."""
    return request.client.host if request.client else "unknown"


def _prune(q: deque[float], now: float) -> None:
    while q and now - q[0] > WINDOW:
        q.popleft()


def locked_out(key: str) -> bool:
    with _FAILS_LOCK:
        q = _FAILS[key]
        _prune(q, time.monotonic())
        return len(q) >= MAX_FAILS


def record_failure(key: str) -> None:
    with _FAILS_LOCK:
        q = _FAILS[key]
        now = time.monotonic()
        _prune(q, now)
        q.append(now)


def clear_failures(key: str | None = None) -> None:
    """Forget failures for one address, or for all when key is None."""
    with _FAILS_LOCK:
        if key is None:
            _FAILS.clear()
        else:
            _FAILS.pop(key, None)


class NotAuthenticated(Exception):
    """Raised by the require_user dependency when no session is present."""


def require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise NotAuthenticated()
    return user
