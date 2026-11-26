"""Shared fixtures.

The app reads its settings from the environment at import time (session
secret, scheduler timezone), so the environment is set here before any test
imports `app.main`.
"""
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEST_USER = "admin"
TEST_PASSWORD = "test-password-not-a-placeholder"

os.environ.update({
    "ADMIN_USERNAME": TEST_USER,
    "ADMIN_PASSWORD": TEST_PASSWORD,
    "SECRET_KEY": "0123456789abcdef0123456789abcdef0123456789abcdef",
    "GITHUB_TOKEN": "",
    "TZ": "UTC",
})

_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_from(html: str) -> str:
    """Pull the hidden CSRF field out of a rendered page."""
    match = _CSRF_RE.search(html)
    assert match, "page has no csrf_token field"
    return match.group(1)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A signed-out client on a fresh database in a temp folder."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("MIRROR_ROOT", str(tmp_path / "mirrors"))
    monkeypatch.setenv("PROTECTION_ROOT", str(tmp_path / "protection"))

    from app import auth
    from app.config import get_settings
    get_settings.cache_clear()
    auth.clear_failures()

    from app.main import app
    with TestClient(app, follow_redirects=False) as c:
        yield c
    get_settings.cache_clear()


def login(client: TestClient, password: str = TEST_PASSWORD) -> str:
    """Sign in and return the CSRF token of the signed-in session."""
    token = csrf_from(client.get("/login").text)
    resp = client.post("/login", data={
        "username": TEST_USER, "password": password, "csrf_token": token,
    })
    assert resp.status_code == 303, resp.text
    return csrf_from(client.get("/settings").text)


@pytest.fixture
def authed(client: TestClient) -> tuple[TestClient, str]:
    """A signed-in client plus its CSRF token."""
    return client, login(client)
