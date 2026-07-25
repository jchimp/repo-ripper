"""The three unit-of-work jobs, callable from the scheduler or a manual trigger.

Each job type holds a lock so a manual run can't collide with a scheduled one.
A blocked run is recorded as 'skipped' rather than silently dropped.
"""
import threading
import time

from . import db, github, mirror, notify, protection

_LOCKS = {
    "scan": threading.Lock(),
    "pull": threading.Lock(),
    "protection": threading.Lock(),
}


def _skip(job_type: str) -> None:
    jid = db.start_job(job_type)
    db.finish_job(jid, "skipped", 0, 0, "Already running \u2014 skipped", "")


def run_scan_job() -> None:
    lock = _LOCKS["scan"]
    if not lock.acquire(blocking=False):
        return _skip("scan")
    try:
        jid = db.start_job("scan")
        try:
            repos = github.list_all_repos(
                include_forks=db.get_bool("include_forks"),
                include_archived=db.get_bool("include_archived"),
            )
        except Exception as exc:
            summary = f"Discovery failed: {exc}"
            db.finish_job(jid, "error", 0, 1, summary, str(exc))
            notify.notify_job("scan", False, summary)
            return

        now = time.time()
        for r in repos:
            db.upsert_repo({
                "full_name": r["full_name"],
                "private": r["private"],
                "fork": r["fork"],
                "archived": r["archived"],
                "default_branch": r["default_branch"],
                "mirror_path": f"{r['full_name']}.git",
                "discovered_at": now,
            })
        summary = f"Discovered {len(repos)} repositories"
        db.finish_job(jid, "ok", len(repos), 0, summary, "")
        notify.notify_job("scan", True, summary)
    finally:
        lock.release()


def run_pull_job() -> None:
    lock = _LOCKS["pull"]
    if not lock.acquire(blocking=False):
        return _skip("pull")
    try:
        jid = db.start_job("pull")
        fetch_lfs = db.get_bool("fetch_lfs")
        include_wikis = db.get_bool("include_wikis")

        repos = db.list_repos(enabled_only=True)
        ok = fail = 0
        lines: list[str] = []
        for r in repos:
            # has_wiki isn't stored; re-derive is cheap enough to skip — attempt
            # the wiki mirror and treat "absent" as non-fatal.
            success, msg, size = mirror.mirror_repo(
                r["full_name"], fetch_lfs=fetch_lfs,
                include_wiki=include_wikis, has_wiki=include_wikis,
            )
            status = "ok" if success else "error"
            db.record_repo_result(r["full_name"], status, None if success else msg, size)
            lines.append(f"[{status}] {r['full_name']} \u2014 {msg}")
            if success:
                ok += 1
            else:
                fail += 1

        overall = "ok" if fail == 0 else ("partial" if ok else "error")
        summary = f"Mirrored {ok} ok, {fail} failed ({len(repos)} enabled)"
        db.finish_job(jid, overall, ok, fail, summary, "\n".join(lines))
        notify.notify_job("pull", fail == 0, summary)
    finally:
        lock.release()


def run_protection_job() -> None:
    lock = _LOCKS["protection"]
    if not lock.acquire(blocking=False):
        return _skip("protection")
    try:
        jid = db.start_job("protection")
        delete = db.get_bool("protection_delete")
        success, detail = protection.run_protection_copy(delete=delete)
        status = "ok" if success else "error"
        summary = ("Protection copy complete" if success
                   else "Protection copy failed")
        db.finish_job(jid, status, 1 if success else 0, 0 if success else 1,
                      summary, detail)
        notify.notify_job("protection", success, summary)
    finally:
        lock.release()


JOBS = {
    "scan": run_scan_job,
    "pull": run_pull_job,
    "protection": run_protection_job,
}
