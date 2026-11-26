"""Dashboard, job detail, and manual job triggers."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import db, jobs, scheduler
from ..web import render

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    repos = db.list_repos()
    enabled = [r for r in repos if r["enabled"]]
    total_size = sum((r["size_bytes"] or 0) for r in repos)
    return render(request, "dashboard.html", {
        "jobs": db.recent_jobs(15),
        "job_specs": list(jobs.JOBS.values()),
        "next_runs": scheduler.next_runs(),
        "repo_count": len(repos),
        "enabled_count": len(enabled),
        "total_size": total_size,
    })


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    job = db.get_job(job_id)
    if not job:
        return RedirectResponse("/", status_code=303)
    return render(request, "job_detail.html", {"job": job})


@router.post("/run/{job_type}")
def run_now(request: Request, job_type: str):
    if job_type in jobs.JOBS:
        scheduler.trigger_now(job_type)
    if request.headers.get("HX-Request"):
        resp = Response(status_code=204)
        resp.headers["HX-Redirect"] = "/"
        return resp
    return RedirectResponse("/", status_code=303)
