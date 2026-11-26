"""The three unit-of-work jobs, callable from the scheduler or a manual trigger.

Each job type holds a lock so a manual run can't collide with a scheduled one.
A blocked run is recorded as 'skipped' rather than silently dropped.
"""
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from . import db, github, mirror, notify, protection, runtime, setup_state, watch

# One lock per job key, created on first use.
_LOCKS: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)


def _skip(job_type: str) -> None:
    jid = db.start_job(job_type)
    db.finish_job(jid, "skipped", 0, 0, "Already running \u2014 skipped", "")


def run_scan_job() -> None:
    lock = _LOCKS["scan"]
    if not lock.acquire(blocking=False):
        return _skip("scan")
    try:
        jid = db.start_job("scan")
        # Keeps the token-health banner on the dashboard current. Never raises.
        setup_state.refresh_token_check()
        # The scan has independent parts: the token's own list, then each
        # watched owner. A failed part does not stop the other parts.
        found = added = parts_ok = parts_failed = 0
        notes: list[str] = []
        lines: list[str] = []

        if not runtime.github_token():
            notes.append("My repositories: skipped, because no GitHub token is set.")
            lines.append("[skipped] my repositories — no GitHub token is set")
        else:
            try:
                repos = github.list_all_repos(
                    include_forks=db.get_bool("include_forks"),
                    include_archived=db.get_bool("include_archived"),
                )
            except Exception as exc:
                parts_failed += 1
                notes.append(f"My repositories: failed ({exc})")
                lines.append(f"[error] my repositories — {exc}")
            else:
                now = time.time()
                new = 0
                # Applies to NEW rows only: a known repository keeps its switch.
                auto_enable = db.get_bool("auto_enable_new", True)
                for r in repos:
                    new += db.upsert_repo({
                        "full_name": r["full_name"],
                        "private": r["private"],
                        "fork": r["fork"],
                        "archived": r["archived"],
                        "default_branch": r["default_branch"],
                        "mirror_path": f"{r['full_name']}.git",
                        "discovered_at": now,
                        "source": "mine",
                    }, enabled=auto_enable)
                parts_ok += 1
                found += len(repos)
                added += new
                lines.append(f"[ok] my repositories — {len(repos)} found, {new} new")

        owners_failed = 0
        rate_limited: github.RateLimited | None = None
        for owner in db.list_watch_owners():
            label = f"watched owner {owner['login']}"
            try:
                # After a rate-limit answer each more call fails the same
                # way and uses up nothing but time.
                if rate_limited:
                    raise rate_limited
                o_found, o_new = watch.discover_owner(owner)
            except github.RateLimited as exc:
                rate_limited = exc
                error = str(exc)
            except github.GitHubError as exc:
                error = str(exc)
            else:
                db.record_watch_owner_scan(owner["id"], None)
                parts_ok += 1
                found += o_found
                added += o_new
                lines.append(f"[ok] {label} — {o_found} found, {o_new} new")
                continue
            db.record_watch_owner_scan(owner["id"], error)
            parts_failed += 1
            owners_failed += 1
            lines.append(f"[error] {label} — {error}")
        if owners_failed:
            notes.append(f"Watched owners that failed: {owners_failed}.")

        if parts_failed == 0:
            status = "ok"
        else:
            status = "partial" if parts_ok else "error"
        summary = " ".join([f"Discovered {found} repositories ({added} new).", *notes])
        db.finish_job(jid, status, found, parts_failed, summary, "\n".join(lines))
        notify.notify_job("scan", parts_failed == 0, summary)
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


@dataclass(frozen=True)
class JobSpec:
    """One schedulable job. The dashboard, the settings page and the scheduler
    are all built from JOBS, so a new job is one `register()` call."""
    key: str            # also the job_runs.job_type and the lock name
    label: str          # button / heading text
    description: str    # one line under the button
    run: Callable[[], None]
    default_cron: str   # used when the settings table has no schedule yet

    @property
    def schedule_key(self) -> str:
        return f"schedule_{self.key}"


JOBS: dict[str, JobSpec] = {}


def register(spec: JobSpec) -> None:
    JOBS[spec.key] = spec


register(JobSpec("scan", "Scan GitHub",
                 "Discover repositories and enrol new ones.",
                 run_scan_job, "0 3 * * *"))
register(JobSpec("pull", "Pull mirrors",
                 "git --mirror every enabled repo to the NAS.",
                 run_pull_job, "30 3 * * *"))
register(JobSpec("protection", "Protection copy",
                 "rsync the mirror share to the second share.",
                 run_protection_job, "0 5 * * 0"))
