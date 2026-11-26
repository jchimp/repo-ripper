"""Runtime values that jobs need: the GitHub token and the storage roots.

All code reads them through these functions, never from `get_settings()`
directly, so that the place they are stored can change in one file.
"""
from pathlib import Path

from .config import get_settings


def github_token() -> str:
    """Return the GitHub token, or an empty string when none is configured."""
    return get_settings().github_token


def mirror_root() -> Path:
    """Folder that holds the bare mirrors (`<owner>/<repo>.git`)."""
    return Path(get_settings().mirror_root)


def protection_root() -> Path:
    """Folder that receives the protection copy of the mirror root."""
    return Path(get_settings().protection_root)
