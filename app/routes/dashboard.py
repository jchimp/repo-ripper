"""Dashboard, job detail, live progress, and manual job triggers."""
import sqlite3
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import db, jobs, scheduler, setup_state, viewhelpers
from ..web import render

router = APIRouter()

# Seconds that "Run now" waits for the job run to appear, so that the page it
# sends the browser to already shows the job as running.
START_WAIT = 1.5
ATTENTION_LIMIT = 12


def _health(repos: list[sqlite3.Row], now: float) -> dict:
    """Sort the repositories into the groups of the health line and the list."""
    active = [r for r in repos if r["enabled"] and r["upstream_state"] != "gone"]
    failing = [r for r in active if r["last_status"] == "error"]
    warn = [r for r in active if r["last_status"] == "warn"]
    never = [r for r in active if r["last_sync_at"] is None]
    limit = now - jobs.STALE_AFTER
    stale = [r for r in active
             if r["last_status"] not in ("error", "warn")
             and ((r["last_ok_at"] and r["last_ok_at"] < limit)
                  or (r["last_sync_at"] is None and (r["discovered_at"] or now) < limit))]
    gone = [r for r in repos if r["upstream_state"] == "gone"]
    verify_failed = [r for r in repos if r["last_verify_status"] == "error"]

    attention: list[dict] = []
    for r in failing:
        attention.append({"repo": r, "kind": "error", "label": "failed",
                          "text": _first_line(r["last_error"])})
    for r in verify_failed:
        attention.append({"repo": r, "kind": "error", "label": "verify failed",
                          "text": _first_line(r["last_verify_message"])})
    for r in warn:
        attention.append({"repo": r, "kind": "warn", "label": "warning",
                          "text": _first_line(r["last_error"])})
    for r in stale:
        text = ("Never synced." if r["last_sync_at"] is None else
                f"Last good sync: {viewhelpers.rel_time(r['last_ok_at'], now)}.")
        attention.append({"repo": r, "kind": "stale", "label": "stale", "text": text})

    return {
        "failing": len(failing), "warn": len(warn), "never": len(never),
        "stale": len(stale), "gone": len(gone), "verify_failed": len(verify_failed),
        "all_good": bool(repos) and not (failing or warn or never or stale or gone
                                         or verify_failed),
        "attention": attention[:ATTENTION_LIMIT],
        "attention_more": max(0, len(attention) - ATTENTION_LIMIT),
    }


def _first_line(text: str | None) -> str:
    """Return the line of a git message that says the most: 'fatal:', else the first."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    best = next((line for line in lines if line.startswith(("fatal:", "error:"))), lines[0])
    return best[:200]


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if not setup_state.is_complete():
        return RedirectResponse("/setup", status_code=303)  # first run
    repos = db.list_repos()
    enabled = [r for r in repos if r["enabled"]]
    total_size = sum((r["size_bytes"] or 0) for r in repos)
    history = db.history_snapshots()
    return render(request, "dashboard.html", {
        "jobs": db.recent_jobs(15),
        "job_specs": list(jobs.JOBS.values()),
        "next_runs": scheduler.next_runs(),
        "repo_count": len(repos),
        "enabled_count": len(enabled),
        "total_size": total_size,
        "token_health": setup_state.token_health(),
        "health": _health(repos, time.time()),
        "live_jobs": viewhelpers.live_jobs(),
        "running_types": {key for key in jobs.JOBS if jobs.is_running(key)},
        "history": history,
        "chart": viewhelpers.size_chart([(h["recorded_at"], h["total_bytes"])
                                         for h in history]),
    })


@router.get("/live", response_class=HTMLResponse)
def live_panel(request: Request):
    """The polled "running now" panel. The page polls it only while a job runs."""
    live = viewhelpers.live_jobs()
    resp = render(request, "_live_panel.html", {"live_jobs": live})
    if not live:
        # The last job is done: load the whole dashboard again, one time, so
        # that the cards, the lists and the chart show the result.
        resp.headers["HX-Refresh"] = "true"
    return resp


def _job_ctx(job: sqlite3.Row) -> dict:
    live = next((lj for lj in viewhelpers.live_jobs() if lj["job"]["id"] == job["id"]), None)
    spec = jobs.JOBS.get(job["job_type"])
    return {
        "job": job,
        "lj": live,
        "job_label": spec.label if spec else job["job_type"],
        "results": [] if live else db.job_repo_results(job["id"]),
    }


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    job = db.get_job(job_id)
    if not job:
        return RedirectResponse("/", status_code=303)
    return render(request, "job_detail.html", _job_ctx(job))


@router.get("/jobs/{job_id}/live", response_class=HTMLResponse)
def job_live(request: Request, job_id: int):
    """The polled part of the job page while the job runs."""
    job = db.get_job(job_id)
    if not job:
        return Response(status_code=204, headers={"HX-Redirect": "/"})
    ctx = _job_ctx(job)
    resp = render(request, "_job_live.html", ctx)
    if not ctx["lj"]:
        resp.headers["HX-Refresh"] = "true"   # done: show the result page
    return resp


def _wait_for_start(last_id: int) -> None:
    deadline = time.monotonic() + START_WAIT
    while time.monotonic() < deadline:
        if db.max_job_id() > last_id:
            return
        time.sleep(0.05)


@router.post("/run/{job_type}")
def run_now(request: Request, job_type: str):
    if job_type in jobs.JOBS:
        last_id = db.max_job_id()
        scheduler.trigger_now(job_type)
        _wait_for_start(last_id)
    if request.headers.get("HX-Request"):
        resp = Response(status_code=204)
        resp.headers["HX-Redirect"] = "/"
        return resp
    return RedirectResponse("/", status_code=303)
