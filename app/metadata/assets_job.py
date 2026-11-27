"""The scheduled job that downloads release files for the selected repositories."""
from .. import db, jobs, notify, runtime, storage_guard
from . import assets
from . import job as _metadata_job  # noqa: F401  (its job shows first: it runs first)
from .client import GitHubClient, format_time

JOB_KEY = "assets"
DEFAULT_KEEP = 10
DEFAULT_MAX_MB = 2048
MAX_MB_LIMIT = 102400   # 100 GB: a guard against a typing error, not a real limit


def _int_setting(key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(db.get_setting(key) or default)
    except ValueError:
        value = default
    return max(low, min(high, value))


def keep_releases() -> int:
    """Newest releases per repository that get downloads. 0 = all releases."""
    return _int_setting("assets_keep_releases", DEFAULT_KEEP, 0, 100000)


def max_mb() -> int:
    """Size limit for one release file, in MB."""
    return _int_setting("assets_max_mb", DEFAULT_MAX_MB, 1, MAX_MB_LIMIT)


def fmt_bytes(n: int) -> str:
    """Short size text for job summaries."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def run_assets_job() -> None:
    """Download release files for each repository that has the switch on.

    Repositories run one after the other, the oldest run first. A rate limit
    or a full disk stops the job; the next run continues.
    """
    lock = jobs._LOCKS[JOB_KEY]
    if not lock.acquire(blocking=False):
        return jobs._skip(JOB_KEY)
    try:
        jid = db.start_job(JOB_KEY)
        repos = db.list_repos_for_assets()
        if not repos:
            db.finish_job(
                jid, "skipped", 0, 0,
                "No repository has the release files switch on. Turn it on in the "
                "Repositories table (the repository also needs the metadata switch).", "")
            return
        problem = storage_guard.mirror_problem()
        if problem:
            jobs.refuse(jid, JOB_KEY, problem)
            return
        try:
            status, ok, fail, summary, detail = _download_all(jid, repos)
        except Exception as exc:  # the run row must not stay on "running"
            summary = f"Release files job failed: {type(exc).__name__}: {exc}"
            db.finish_job(jid, "error", 0, 1, summary, "")
            notify.notify_job(JOB_KEY, False, summary)
            return
        db.finish_job(jid, status, ok, fail, summary, detail)
        notify.notify_job(JOB_KEY, fail == 0, summary)
    finally:
        lock.release()


def _download_all(jid: int, repos: list) -> tuple[str, int, int, str, str]:
    """Run the downloads. Returns (job status, ok count, fail count, summary, detail)."""
    keep, limit = keep_releases(), max_mb() * 1024 * 1024
    total = len(repos)
    ok = partial = fail = files = size = 0
    lines: list[str] = []
    stopped: assets.AssetsResult | None = None

    with GitHubClient() as client:
        for r in repos:
            name = r["full_name"]
            try:
                res = assets.sync_repo(client, name, keep_releases=keep, max_bytes=limit)
            except Exception as exc:
                # One repository must not stop the others.
                res = assets.AssetsResult("error", f"{type(exc).__name__}: {exc}")
                db.record_assets_result(name, res.status, res.message,
                                        r["assets_files"] or 0, r["assets_bytes"] or 0)
            else:
                db.record_assets_result(name, res.status, res.message, res.files,
                                        res.total_bytes)
            lines.append(f"[{res.status}] {name} — {res.message}")
            files += res.downloaded
            size += res.downloaded_bytes
            if res.status == "ok":
                ok += 1
            elif res.status == "partial":
                partial += 1
            elif res.status == "rate_limited" or "The disk has" in res.message:
                stopped = res
                break
            else:
                fail += 1
            db.update_job_progress(jid, ok, fail, "\n".join(lines))

    got = f"{files} files downloaded ({fmt_bytes(size)})."
    if stopped is not None:
        done = ok + partial + fail
        if stopped.status == "rate_limited":
            reset = format_time(stopped.reset_at) if stopped.reset_at else "an unknown time"
            why = f"GitHub rate limit reached. The limit resets at {reset}."
            if not runtime.github_token():
                why += " No GitHub token is set: the limit is 60 requests in one hour."
            return ("partial", ok, fail,
                    f"{why} {done} of {total} repositories done. {got} The next run continues.",
                    "\n".join(lines))
        return ("error", ok, fail + 1,
                f"Stopped: {stopped.message} {done} of {total} repositories done. {got}",
                "\n".join(lines))

    summary = (f"Release files: {ok} ok, {partial} partial, {fail} failed "
               f"({total} selected). {got}")
    if fail == 0 and partial == 0:
        status = "ok"
    else:
        status = "partial" if ok or partial else "error"
    return status, ok, fail, summary, "\n".join(lines)


jobs.register(jobs.JobSpec(
    JOB_KEY, "Download release files",
    "Download attached files and source archives of the newest releases.",
    run_assets_job, "30 4 * * *",
))
