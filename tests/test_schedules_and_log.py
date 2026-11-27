"""Schedule checks on save and at start, "run now" times, and the process log."""
import logging
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

BOT_TOKEN = "123456:SECRET-bot-token-value"


# --- cron strings ------------------------------------------------------------

@pytest.mark.parametrize("text", ["", "   ", "0 4 * * *", "*/15 2-5 * * mon-fri"])
def test_cron_problem_accepts_valid_and_empty(text: str) -> None:
    from app import scheduler
    assert scheduler.cron_problem(text) is None


@pytest.mark.parametrize("text", ["30 3 * *", "99 * * * *", "a b c d e", "0 4 * * * *"])
def test_cron_problem_rejects_bad_strings(text: str) -> None:
    from app import scheduler
    assert scheduler.cron_problem(text) == scheduler.CRON_PROBLEM


def test_bad_cron_on_save_stores_nothing(authed: tuple[TestClient, str]) -> None:
    from app import db, scheduler
    client, token = authed
    client.post("/settings/schedules", data={"csrf_token": token, "schedule_scan": "0 4 * * *"})
    before = db.get_all_settings()
    next_before = scheduler.next_runs()

    resp = client.post("/settings/schedules", data={
        "csrf_token": token,
        "schedule_scan": "30 3 * *",          # four fields
        "schedule_pull": "15 5 * * *",        # valid, but in the same form
    })

    assert resp.status_code == 400
    assert db.get_all_settings() == before
    assert scheduler.next_runs() == next_before
    html = resp.text
    assert "Schedules not saved." in html
    assert 'href="/settings/schedules" class="active"' in html   # stays on the tab
    # One message, next to the bad field only.
    assert html.count(scheduler.CRON_PROBLEM) == 1
    assert 'id="err-schedule_scan"' in html
    assert 'id="err-schedule_pull"' not in html
    # The typed values stay in the fields.
    assert 'name="schedule_scan"\n                 value="30 3 * *"' in html
    assert 'value="15 5 * * *"' in html


def test_good_save_stores_all_values(authed: tuple[TestClient, str]) -> None:
    from app import db, scheduler
    client, token = authed
    resp = client.post("/settings/schedules", data={
        "csrf_token": token, "schedule_scan": " 10 4 * * * ", "schedule_pull": "",
    })
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings/schedules?saved=schedules"
    resp = client.post("/settings/discovery", data={
        "csrf_token": token, "include_forks": "on", "pull_workers": "99",
    })
    assert resp.headers["location"] == "/settings/discovery?saved=1"
    client.post("/settings/notifications", data={
        "csrf_token": token, "telegram_chat_id": "chat-1", "telegram_bot_token": BOT_TOKEN,
    })
    assert db.get_setting("schedule_scan") == "10 4 * * *"
    assert db.get_setting("schedule_pull") == ""
    assert db.get_bool("include_forks") is True
    assert db.get_bool("include_archived") is False
    assert db.get_setting("telegram_chat_id") == "chat-1"
    assert db.get_setting("telegram_bot_token") == BOT_TOKEN
    assert db.get_setting("pull_workers") == "8"   # limited to 1..8
    runs = scheduler.next_runs()
    assert runs["scan"] is not None
    assert runs["pull"] is None


def test_bad_stored_cron_falls_back_to_default(
        client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    from app import db, jobs, scheduler
    spec = jobs.JOBS["pull"]
    assert spec.default_cron.strip(), "the test needs a job with a default schedule"
    db.set_setting(spec.schedule_key, "30 3 * *")
    scheduler.scheduler.remove_job(spec.key)   # the state after a restart: no job yet

    with caplog.at_level(logging.WARNING, logger="repo-ripper"):
        scheduler.configure_jobs()

    assert scheduler.next_runs()[spec.key] is not None
    assert db.get_setting(spec.schedule_key) == spec.default_cron
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(spec.key in m and "'30 3 * *'" in m for m in warnings)


# --- "run now" ---------------------------------------------------------------

def test_run_now_uses_an_aware_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import scheduler
    calls: list[dict] = []
    monkeypatch.setattr(scheduler.scheduler, "add_job",
                        lambda *args, **kwargs: calls.append(kwargs))

    scheduler.trigger_now("scan")
    scheduler.run_in_background(print, "x")

    assert len(calls) == 2
    for kwargs in calls:
        run_date: datetime = kwargs["run_date"]
        assert run_date.tzinfo is not None
        assert run_date.utcoffset() == datetime.now(scheduler.scheduler.timezone).utcoffset()


# --- process log -------------------------------------------------------------

def test_log_setup_keeps_url_loggers_quiet() -> None:
    import app.main  # noqa: F401  (the import sets up the log)
    from app import logsetup

    assert logging.getLogger().level == logging.INFO
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
    # A second call adds no second handler: no line is written two times.
    logsetup.configure()
    ours = [h for h in logging.getLogger().handlers if isinstance(h, logsetup._StderrHandler)]
    assert len(ours) == 1


def _telegram_on() -> None:
    from app import db
    db.set_settings({
        "telegram_enabled": "1", "telegram_bot_token": BOT_TOKEN, "telegram_chat_id": "42",
    })


def _post_through(handler) -> object:  # noqa: ANN001
    """A stand-in for httpx.post that uses the real client code, with no network."""
    def post(url: str, **kwargs: object) -> httpx.Response:
        kwargs.pop("timeout", None)
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            return c.post(url, **kwargs)
    return post


def _all_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(f"{r.getMessage()} {r.exc_text or ''}" for r in caplog.records)


def test_telegram_token_does_not_reach_the_log(
        client: TestClient, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    from app import notify
    _telegram_on()
    seen: list[str] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False})

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"no route to {request.url}", request=request)

    # No caplog.set_level here: the test reads the log at the levels of the app.
    monkeypatch.setattr(notify.httpx, "post", _post_through(ok))
    notify.send("hello")
    assert notify.test_message() is True
    assert seen and BOT_TOKEN in seen[0]     # the request did go through httpx

    monkeypatch.setattr(notify.httpx, "post", _post_through(refused))
    notify.send("hello")
    monkeypatch.setattr(notify.httpx, "post", _post_through(broken))
    notify.send("hello")
    assert notify.test_message() is False

    text = _all_text(caplog)
    assert BOT_TOKEN not in text
    assert "api.telegram.org" not in text
    assert "HTTP status 401" in text
    assert text.count("ConnectError") == 2


def test_scheduler_writes_job_lines(caplog: pytest.LogCaptureFixture) -> None:
    from apscheduler.events import (
        EVENT_JOB_ERROR,
        EVENT_JOB_EXECUTED,
        EVENT_JOB_SUBMITTED,
        JobExecutionEvent,
        JobSubmissionEvent,
    )

    from app import scheduler
    now = datetime.now(scheduler.scheduler.timezone)
    with caplog.at_level(logging.INFO, logger="repo-ripper"):
        scheduler._log_job_event(JobSubmissionEvent(EVENT_JOB_SUBMITTED, "pull", "default", [now]))
        scheduler._log_job_event(JobExecutionEvent(EVENT_JOB_EXECUTED, "pull", "default", now))
        scheduler._log_job_event(JobExecutionEvent(
            EVENT_JOB_ERROR, "pull", "default", now,
            exception=RuntimeError(f"https://x/bot{BOT_TOKEN}/send")))
    lines = [r.getMessage() for r in caplog.records]
    assert lines == ["job pull started", "job pull finished", "job pull raised RuntimeError"]
