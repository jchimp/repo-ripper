"""The Jobs page: all job runs with filters and pages, and the job run page."""
import re

from fastapi.testclient import TestClient

from app import db
from app.routes import job_runs


def _run(job_type: str, status: str, summary: str = "") -> int:
    jid = db.start_job(job_type)
    if status != "running":
        db.finish_job(jid, status, 0, 0, summary or f"{job_type} {status}", "")
    return jid


def _run_ids(html: str) -> list[int]:
    return [int(m) for m in re.findall(r'href="/jobs/(\d+)"', html)]


def test_jobs_page_lists_all_runs_newest_first(authed: tuple[TestClient, str]) -> None:
    client, _ = authed
    first = _run("scan", "ok")
    second = _run("pull", "error", "2 repositories failed")
    html = client.get("/jobs").text
    assert _run_ids(html) == [second, first]
    assert "Pull mirrors" in html and "Scan GitHub" in html   # labels, not job keys
    assert "2 repositories failed" in html
    assert 'class="active">Jobs</a>' in html                   # header navigation


def test_jobs_page_without_runs(authed: tuple[TestClient, str]) -> None:
    client, _ = authed
    html = client.get("/jobs").text
    assert "No jobs yet." in html and 'rel="next"' not in html


def test_filters_by_job_and_status(authed: tuple[TestClient, str]) -> None:
    client, _ = authed
    scan_ok = _run("scan", "ok")
    _run("scan", "error")
    pull_ok = _run("pull", "ok")
    assert _run_ids(client.get("/jobs?type=scan&status=ok").text) == [scan_ok]
    assert _run_ids(client.get("/jobs?status=ok").text) == [pull_ok, scan_ok]
    html = client.get("/jobs?type=verify").text
    assert _run_ids(html) == [] and "No job run matches the filters." in html


def test_unknown_filter_values_show_all_runs(authed: tuple[TestClient, str]) -> None:
    client, _ = authed
    a, b = _run("scan", "ok"), _run("pull", "ok")
    html = client.get("/jobs?type=nope';--&status=weird&page=-3").text
    assert _run_ids(html) == [b, a]
    assert " selected" not in html          # both drop-downs show "All"


def test_pages_of_runs(authed: tuple[TestClient, str], monkeypatch) -> None:
    client, _ = authed
    monkeypatch.setattr(job_runs, "PAGE_SIZE", 3)
    ids = [_run("scan", "ok") for _ in range(7)]
    page1 = client.get("/jobs?type=scan").text
    assert _run_ids(page1) == ids[::-1][:3]
    assert "1–3 of 7" in page1 and 'href="/jobs?type=scan&amp;page=2" rel="next"' in page1
    page3 = client.get("/jobs?type=scan&page=3").text
    assert _run_ids(page3) == [ids[0]]
    assert 'href="/jobs?type=scan&amp;page=2" rel="prev"' in page3 and 'rel="next"' not in page3


def test_filter_form_gets_only_the_list(authed: tuple[TestClient, str]) -> None:
    client, _ = authed
    jid = _run("scan", "ok")
    resp = client.get("/jobs?type=scan", headers={"HX-Request": "true", "HX-Target": "job-list"})
    assert '<div id="job-list">' in resp.text and "<html" not in resp.text
    assert _run_ids(resp.text) == [jid]


def test_running_job_shows_the_live_panel(authed: tuple[TestClient, str]) -> None:
    client, _ = authed
    assert 'hx-get="/live"' not in client.get("/jobs").text   # no job: no polling
    jid = _run("pull", "running")
    html = client.get("/jobs").text
    assert 'hx-get="/live"' in html and "Running now" in html
    assert jid in _run_ids(html)


def test_unknown_job_run_goes_to_the_jobs_page(authed: tuple[TestClient, str]) -> None:
    client, _ = authed
    resp = client.get("/jobs/999")
    assert resp.status_code == 303 and resp.headers["location"] == "/jobs"


def test_job_run_page_links_back_to_jobs(authed: tuple[TestClient, str]) -> None:
    client, _ = authed
    jid = _run("scan", "ok")
    html = client.get(f"/jobs/{jid}").text
    assert f'<a href="/jobs">Jobs</a> / Run {jid}' in html
    assert 'class="active">Jobs</a>' in html


def test_dashboard_links_to_jobs_and_has_no_job_list(authed: tuple[TestClient, str]) -> None:
    client, _ = authed
    _run("scan", "ok", "found 3")
    html = client.get("/").text
    assert 'href="/jobs">All job runs</a>' in html
    assert "Recent jobs" not in html and "found 3" not in html
