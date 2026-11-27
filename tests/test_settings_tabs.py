"""Settings tabs: one URL and one form for each section; a save touches only its keys."""
import re

import pytest
from fastapi.testclient import TestClient

from app import db
from app.routes.settings import TABS

FORM_TABS = [tab for tab in TABS if tab.slug != "storage"]


def test_settings_redirects_to_the_first_tab(authed: tuple[TestClient, str]) -> None:
    client, _ = authed
    resp = client.get("/settings")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings/storage"


@pytest.mark.parametrize("slug", [tab.slug for tab in TABS])
def test_each_tab_renders_and_marks_itself(authed: tuple[TestClient, str], slug: str) -> None:
    client, _ = authed
    resp = client.get(f"/settings/{slug}")
    assert resp.status_code == 200
    assert f'href="/settings/{slug}" class="active" aria-current="page"' in resp.text
    for tab in TABS:
        assert f'href="/settings/{tab.slug}"' in resp.text


@pytest.mark.parametrize("tab", FORM_TABS, ids=lambda t: t.slug)
def test_each_form_posts_to_its_tab_with_csrf(authed: tuple[TestClient, str], tab) -> None:
    client, _ = authed
    html = client.get(f"/settings/{tab.slug}").text
    form = re.search(rf'<form method="post" action="/settings/{tab.slug}">\s*'
                     r'<input type="hidden" name="csrf_token" value="[^"]+">', html)
    assert form, tab.slug
    assert client.post(f"/settings/{tab.slug}", data={}).status_code == 403


def test_unknown_section_is_404(authed: tuple[TestClient, str]) -> None:
    client, token = authed
    assert client.get("/settings/nope").status_code == 404
    assert client.post("/settings/nope", data={"csrf_token": token}).status_code == 404


@pytest.mark.parametrize("saved", FORM_TABS, ids=lambda t: t.slug)
def test_a_save_writes_only_the_keys_of_its_tab(authed: tuple[TestClient, str], saved) -> None:
    client, token = authed
    # All checkboxes on, then an empty save of one tab: only that tab's go off.
    all_bools = [key for tab in TABS for key in tab.bools]
    db.set_settings({key: "1" for key in all_bools})
    before = db.get_all_settings()
    resp = client.post(f"/settings/{saved.slug}", data={"csrf_token": token})
    assert resp.status_code == 303
    after = db.get_all_settings()
    for key in all_bools:
        assert after[key] == ("0" if key in saved.bools else "1"), key
    changed = {key for key in after if after.get(key) != before.get(key)}
    assert changed <= set(saved.bools), changed
