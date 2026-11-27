"""Jobs page: the running jobs and every recorded job run, plus the page of one run."""
import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import db, jobs, viewhelpers
from ..web import render

router = APIRouter()

PAGE_SIZE = 50


def _jobs_url(job_type: str, status: str, page: int) -> str:
    """Return the Jobs page URL with only the filters that are set."""
    params = {k: v for k, v in (("type", job_type), ("status", status)) if v}
    if page > 1:
        params["page"] = str(page)
    return f"/jobs?{urlencode(params)}" if params else "/jobs"


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, type: str = "", status: str = "", page: int = 1):  # noqa: A002
    # Unknown filter values are dropped, not passed to SQL as data that
    # matches nothing: a stale bookmark then shows all runs.
    job_type = type if type in jobs.JOBS else ""
    status = status if status in db.JOB_STATUSES else ""
    page = max(1, page)
    rows, total = db.query_jobs(job_type=job_type, status=status, limit=PAGE_SIZE,
                                offset=(page - 1) * PAGE_SIZE)
    pages = max(1, -(-total // PAGE_SIZE))
    first = (page - 1) * PAGE_SIZE + 1
    ctx = {
        "runs": rows,
        "total": total,
        "f": {"type": job_type, "status": status},
        "job_specs": list(jobs.JOBS.values()),
        "labels": {key: spec.label for key, spec in jobs.JOBS.items()},
        "statuses": db.JOB_STATUSES,
        "keep": db.JOB_RUNS_KEEP,
        "live_jobs": viewhelpers.live_jobs(),
        "prev_url": _jobs_url(job_type, status, page - 1) if page > 1 else "",
        "next_url": _jobs_url(job_type, status, page + 1) if page < pages else "",
        "pager_label": f"{first}–{first + len(rows) - 1} of {total}" if rows else "",
    }
    # The filter form swaps only the list; a full load gets the whole page.
    if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "job-list":
        return render(request, "_job_list.html", ctx)
    return render(request, "jobs.html", ctx)


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
        return RedirectResponse("/jobs", status_code=303)
    return render(request, "job_detail.html", _job_ctx(job))


@router.get("/jobs/{job_id}/live", response_class=HTMLResponse)
def job_live(request: Request, job_id: int):
    """The polled part of the job page while the job runs."""
    job = db.get_job(job_id)
    if not job:
        return Response(status_code=204, headers={"HX-Redirect": "/jobs"})
    ctx = _job_ctx(job)
    resp = render(request, "_job_live.html", ctx)
    if not ctx["lj"]:
        resp.headers["HX-Refresh"] = "true"   # done: show the result page
    return resp
