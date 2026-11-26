"""Encryption for small secrets that are stored in the `settings` table.

The key is derived from `SECRET_KEY`, so a copy of the database alone does not
reveal the GitHub token. If `SECRET_KEY` changes, stored values cannot be read
any more: callers get `SecretUnreadable` and treat the secret as not set.
"""
import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import get_settings

# Domain separation: the same SECRET_KEY also signs session cookies. A fixed,
# named `info` value makes this key different from every other derived key.
# Changing this value makes all stored secrets unreadable.
HKDF_INFO = b"repo-ripper/settings-secrets/v1"


class SecretUnreadable(Exception):
    """The stored value cannot be decrypted with the current SECRET_KEY."""


def _fernet(secret_key: str | None = None) -> Fernet:
    material = (secret_key if secret_key is not None else get_settings().secret_key).encode()
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=HKDF_INFO).derive(material)
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(plain: str, secret_key: str | None = None) -> str:
    """Encrypt a string for storage.

    Args:
        plain: The secret text.
        secret_key: Key material. Defaults to the app `SECRET_KEY`.

    Returns:
        URL-safe text that is safe to store in the settings table.
    """
    return _fernet(secret_key).encrypt(plain.encode()).decode()


def decrypt(stored: str, secret_key: str | None = None) -> str:
    """Decrypt a value made by `encrypt`.

    Args:
        stored: The stored text.
        secret_key: Key material. Defaults to the app `SECRET_KEY`.

    Returns:
        The secret text.

    Raises:
        SecretUnreadable: The value is damaged, or `SECRET_KEY` changed.
    """
    try:
        return _fernet(secret_key).decrypt(stored.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise SecretUnreadable("Stored secret cannot be decrypted") from exc
