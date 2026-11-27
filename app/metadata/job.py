"""The scheduled job that captures metadata for every selected repository."""
from .. import db, jobs, notify, runtime, storage_guard
from . import capture, store
from .client import GitHubClient, format_time

JOB_KEY = "metadata"


def run_metadata_job() -> None:
    """Capture metadata for each repository with backup and metadata switched on.

    Repositories run one after the other, the oldest capture first. A rate
    limit stops the job; the next run continues with the remaining ones.
    """
    lock = jobs._LOCKS[JOB_KEY]
    if not lock.acquire(blocking=False):
        return jobs._skip(JOB_KEY)
    try:
        jid = db.start_job(JOB_KEY)
        if not db.get_bool("metadata_enabled", True):
            db.finish_job(jid, "skipped", 0, 0, "Metadata capture is off in Settings.", "")
            return
        problem = storage_guard.mirror_problem()
        if problem:
            jobs.refuse(jid, JOB_KEY, problem)
            return
        try:
            status, ok, fail, summary, detail = _capture_all()
        except Exception as exc:  # the run row must not stay on "running"
            summary = f"Metadata capture failed: {type(exc).__name__}: {exc}"
            db.finish_job(jid, "error", 0, 1, summary, "")
            notify.notify_job(JOB_KEY, False, summary)
            return
        db.finish_job(jid, status, ok, fail, summary, detail)
        notify.notify_job(JOB_KEY, fail == 0, summary)
    finally:
        lock.release()


def _capture_all() -> tuple[str, int, int, str, str]:
    """Run the capture. Returns (job status, ok count, fail count, summary, detail)."""
    repos = db.list_repos_for_metadata()
    total = len(repos)
    ok = partial = fail = fetched = 0
    lines: list[str] = []
    limited: capture.CaptureResult | None = None

    with GitHubClient() as client:
        for r in repos:
            name = r["full_name"]
            try:
                res = capture.capture_repo(client, name)
            except Exception as exc:
                # An invalid name or an answer from GitHub that the code does
                # not expect. One repository must not stop the others.
                res = capture.CaptureResult("error", f"{type(exc).__name__}: {exc}")
            try:
                size = store.meta_bytes(name)
            except ValueError:  # an invalid name: capture_repo reported it already
                size = None
            db.record_metadata_result(name, res.status, res.message, res.issues, res.pulls,
                                      size)
            lines.append(f"[{res.status}] {name} — {res.message}")
            fetched += res.fetched
            if res.status == "rate_limited":
                limited = res
                break
            if res.status == "ok":
                ok += 1
            elif res.status == "partial":
                partial += 1
            else:
                fail += 1

    if limited is not None:
        done = ok + partial + fail
        reset = format_time(limited.reset_at) if limited.reset_at else "an unknown time"
        summary = (f"GitHub rate limit reached. {done} of {total} repositories done, "
                   f"{total - done} remain. The limit resets at {reset}. "
                   f"The next run continues.")
        if not runtime.github_token():
            summary += " No GitHub token is set: the limit is 60 requests in one hour."
        return "partial", ok, fail, summary, "\n".join(lines)

    summary = (f"Metadata captured: {ok} ok, {partial} partial, {fail} failed "
               f"({total} selected). {fetched} items downloaded.")
    if fail == 0 and partial == 0:
        status = "ok"
    else:
        status = "partial" if ok or partial else "error"
    return status, ok, fail, summary, "\n".join(lines)


jobs.register(jobs.JobSpec(
    JOB_KEY, "Capture metadata",
    "Download pull requests, issues, comments and releases as JSON files.",
    run_metadata_job, "0 4 * * *",
))
