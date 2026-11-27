"""The unit-of-work jobs, callable from the scheduler or a manual trigger.

Each job type holds a lock so a manual run can't collide with a scheduled one.
A blocked run is recorded as 'skipped' rather than silently dropped.
"""
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from . import (
    db,
    github,
    mirror,
    notify,
    progress,
    protection,
    runtime,
    setup_state,
    storage_guard,
    watch,
)

# One lock per job key, created on first use.
_LOCKS: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)

# Repositories that a sync or a verify works on now. Two git processes must
# never write to one mirror, also when the jobs are of different types.
_ACTIVE: set[str] = set()
_ACTIVE_LOCK = threading.Lock()

FLUSH_INTERVAL = 3.0        # seconds between writes of the live log to the DB
MAX_FAILED_NAMES = 10       # names in the Telegram text, then "+N more"
STALE_AFTER = 3 * 86400     # seconds without a good sync before "stale"


def _claim(name: str) -> bool:
    with _ACTIVE_LOCK:
        if name in _ACTIVE:
            return False
        _ACTIVE.add(name)
        return True


def _release(name: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.discard(name)


def is_running(job_type: str) -> bool:
    """Return True while the job of this type holds its lock."""
    return _LOCKS[job_type].locked()


def pull_workers() -> int:
    """Return the number of parallel pulls from the settings, limited to 1-8."""
    try:
        value = int(db.get_setting("pull_workers") or 4)
    except ValueError:
        value = 4
    return max(1, min(8, value))


def _skip(job_type: str) -> None:
    jid = db.start_job(job_type)
    db.finish_job(jid, "skipped", 0, 0, "Already running — skipped", "")


def refuse(jid: int, job_type: str, summary: str) -> None:
    """Close a job run that must not do its work, as an error with a notification.

    Args:
        jid: The id of the open job run.
        job_type: The job key, for the notification.
        summary: The reason, in plain text for the user.
    """
    db.finish_job(jid, "error", 0, 1, summary, "")
    notify.notify_job(job_type, False, summary)


def run_scan_job() -> None:
    lock = _LOCKS["scan"]
    if not lock.acquire(blocking=False):
        return _skip("scan")
    try:
        jid = db.start_job("scan")
        try:
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
                    # The full listing, with no filter: the upstream check below
                    # must see forks and archived repositories also when the
                    # settings leave them out of the backup.
                    listing = github.list_all_repos(include_forks=True, include_archived=True)
                except Exception as exc:
                    parts_failed += 1
                    notes.append(f"My repositories: failed ({exc})")
                    lines.append(f"[error] my repositories — {exc}")
                else:
                    # A name from the API becomes a folder name. The same
                    # check as for watched repositories. The upstream check
                    # below must not see such a name either: it moves folders.
                    unsafe = [r for r in listing
                              if not watch.safe_full_name(str(r.get("full_name") or ""))]
                    if unsafe:
                        listing = [r for r in listing if r not in unsafe]
                        notes.append(f"Skipped {len(unsafe)} with a name that is not "
                                     "a valid owner/name.")
                        lines.extend(f"[skipped] {r.get('full_name')!r} — the name is not "
                                     "a valid owner/name. It is not added."
                                     for r in unsafe)
                    include_forks = db.get_bool("include_forks")
                    include_archived = db.get_bool("include_archived")
                    repos = [r for r in listing
                             if (include_forks or not r.get("fork"))
                             and (include_archived or not r.get("archived"))]
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
                    # Renamed and gone repositories. Only after a successful listing.
                    upstream_summary, upstream_detail = reconcile_upstream(listing)
                    if upstream_summary:
                        notes.append(f"Upstream changes{upstream_summary}.")
                    if upstream_detail:
                        lines.append(upstream_detail)

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
        except Exception as exc:
            _crash(jid, "scan", exc)
    finally:
        lock.release()


# --- upstream state: renamed and gone repositories ----------------------------

def reconcile_upstream(listing: list[dict]) -> tuple[str, str]:
    """Compare the table with a successful GitHub listing.

    Call it one time, after the listing was stored. It follows renames (by the
    numeric GitHub id), marks repositories that left the listing as gone
    upstream, and clears that mark when one comes back. Only rows with
    `source='mine'` are judged. No mirror is deleted.

    Args:
        listing: The dicts from `github.list_all_repos` (`id`, `full_name`).

    Returns:
        (text to add to the job summary, lines for the job log).
    """
    mine = [r for r in db.list_repos() if r["source"] == "mine"]
    if not listing:
        if not mine:
            return "", ""
        # An empty answer is more probably a token or API problem than the
        # loss of every repository. Marking all as gone would stop the backup.
        text = (f"GitHub returned 0 repositories, but {len(mine)} are known. This is "
                "not normal. No repository was marked as gone upstream. Examine the token.")
        return f". Warning: {text}", text

    notes: list[str] = []
    renamed = 0
    unresolved: set[int] = set()    # rows whose rename could not be stored
    by_name = {r["full_name"]: r for r in db.list_repos()}
    by_gid = {r["github_id"]: r for r in mine if r["github_id"] is not None}
    for item in listing:
        gid, name = item.get("id"), item["full_name"]
        if gid is None:
            continue
        known = by_gid.get(gid)
        if known is None:
            row = by_name.get(name)
            if row is not None and row["source"] == "mine":
                if row["github_id"] is not None:
                    notes.append(f"{name}: the GitHub id changed. The repository was "
                                 "deleted and made again with the same name.")
                db.set_github_id(row["id"], gid)
        elif known["full_name"] != name:
            done, note = _follow_rename(known, name, by_name.get(name))
            notes.append(note)
            if done:
                renamed += 1
            else:
                unresolved.add(known["id"])

    listed = {item["full_name"] for item in listing}
    include_forks = db.get_bool("include_forks")
    include_archived = db.get_bool("include_archived")
    gone = 0
    for r in db.list_repos():
        if r["source"] != "mine" or r["id"] in unresolved:
            continue
        if r["full_name"] in listed:
            if r["upstream_state"] == "gone":
                db.set_upstream_state(r["id"], "ok")
                notes.append(f"{r['full_name']}: back in the listing. It is mirrored again.")
            continue
        # The listing leaves these out because of a setting, not because they
        # are gone.
        if (r["fork"] and not include_forks) or (r["archived"] and not include_archived):
            continue
        gone += 1
        if r["upstream_state"] != "gone":
            db.set_upstream_state(r["id"], "gone")
            notes.append(f"{r['full_name']}: gone upstream (deleted, or the token can no "
                         "longer see it). The mirror stays on disk. The pull job skips it.")

    parts = []
    if renamed:
        parts.append(f"{renamed} renamed")
    if unresolved:
        parts.append(f"{len(unresolved)} rename not applied (see the log)")
    if gone:
        parts.append(f"{gone} gone upstream")
    return (". " + ", ".join(parts) if parts else ""), "\n".join(notes)


def _follow_rename(row: sqlite3.Row, new_name: str,
                   stub: sqlite3.Row | None) -> tuple[bool, str]:
    """Give a row its new name and move its mirror folders. Returns (done, note)."""
    old_name = row["full_name"]
    if stub is not None:
        # The scan has just made a row for the new name. Remove it only if it
        # is that empty row; a row with history is a different repository.
        try:
            removed = db.delete_unsynced_repo(stub["id"])
        except sqlite3.IntegrityError:
            removed = False
        if not removed:
            return False, (f"{old_name} is now {new_name} on GitHub, but a different entry "
                           f"already has the name {new_name}. Nothing was changed.")
    db.rename_repo(row["id"], new_name, f"{new_name}.git")

    root = runtime.mirror_root()
    problems: list[str] = []
    moved = 0
    busy = not _claim(old_name)
    try:
        for suffix in (".git", ".wiki.git", ".meta"):
            src, dst = root / f"{old_name}{suffix}", root / f"{new_name}{suffix}"
            if not src.exists():
                continue
            if busy:
                problems.append(f"{src.name}: a sync is running on it")
            elif dst.exists():
                problems.append(f"{src.name}: the folder {new_name}{suffix} already exists")
            else:
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    src.rename(dst)
                    moved += 1
                except OSError as exc:
                    problems.append(f"{src.name}: {exc}")
    finally:
        if not busy:
            _release(old_name)

    note = f"Renamed upstream: {old_name} is now {new_name}."
    if moved:
        note += f" Moved {moved} folder(s) to the new name."
    if problems:
        note += (" Not moved: " + "; ".join(problems) + ". The old folder stays on disk. "
                 f"The next pull writes to {new_name}.git.")
    return True, note


# --- pull -----------------------------------------------------------------------

def _sync_one(jid: int, r: sqlite3.Row, fetch_lfs: bool, include_wikis: bool,
              claimed: bool = False) -> tuple[str, str]:
    """Mirror one repository and record the result. Returns (full name, status).

    It never raises: one repository that fails must not stop the others.
    """
    name = r["full_name"]
    if not claimed and not _claim(name):
        progress.end_item(jid, name, "skipped",
                          f"[skipped] {name} — a different job works on it now")
        return name, "skipped"
    status = "error"
    line = f"[error] {name} — no result"
    try:
        progress.begin_item(jid, name)
        started = time.time()
        try:
            # has_wiki isn't stored; attempt the wiki mirror and treat "absent"
            # as non-fatal.
            res = mirror.mirror_repo(name, fetch_lfs=fetch_lfs,
                                     include_wiki=include_wikis, has_wiki=include_wikis)
        except Exception as exc:
            res = mirror.MirrorResult(False, "sync",
                                      mirror.scrub(f"unexpected error: {exc!r}"))
        status = res.status
        message = res.message
        if res.warnings:
            message += "\n" + "\n".join(res.warnings)
        line = f"[{status}] {name} — {message}"
        db.record_repo_result(name, status, None if status == "ok" else message,
                              res.size_bytes)
        db.add_sync_log(r["id"], jid, started, time.time(), res.action, status,
                        message, res.size_bytes)
    except Exception as exc:
        # For example the database is locked for longer than the busy timeout.
        status = "error"
        line = f"[error] {name} — the result was not stored: {exc!r}"
    finally:
        _release(name)
        progress.end_item(jid, name, status, line)
    return name, status


def _sync_repos(jid: int, repos: list[sqlite3.Row],
                claimed: bool = False) -> dict[str, list[str]]:
    """Mirror repositories in parallel. Returns the names for each status."""
    fetch_lfs = db.get_bool("fetch_lfs")
    include_wikis = db.get_bool("include_wikis")
    by_status: dict[str, list[str]] = {"ok": [], "warn": [], "error": [], "skipped": []}
    if not repos:
        return by_status
    last_flush = time.monotonic()
    workers = min(pull_workers(), len(repos))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pull") as pool:
        futures = [pool.submit(_sync_one, jid, r, fetch_lfs, include_wikis, claimed)
                   for r in repos]
        for fut in as_completed(futures):
            name, status = fut.result()
            by_status[status].append(name)
            if time.monotonic() - last_flush >= FLUSH_INTERVAL:
                last_flush = time.monotonic()
                _flush_progress(jid)
    for names in by_status.values():
        names.sort(key=str.lower)
    return by_status


def _flush_progress(jid: int) -> None:
    """Write the live counts and log to the job row, so that a reload shows them."""
    snap = progress.snapshot(jid)
    if snap:
        db.update_job_progress(jid, snap["ok"] + snap["warn"], snap["failed"],
                               "\n".join(snap["lines"]))


def failed_names_text(names: list[str]) -> str:
    """Return 'Failed: a, b, c +N more' for a notification, or '' for no names."""
    if not names:
        return ""
    shown = ", ".join(names[:MAX_FAILED_NAMES])
    more = len(names) - MAX_FAILED_NAMES
    return f"\nFailed: {shown}" + (f" +{more} more" if more > 0 else "")


def _counts_text(by_status: dict[str, list[str]]) -> str:
    ok, warn = len(by_status["ok"]), len(by_status["warn"])
    text = f"{ok + warn} ok"
    if warn:
        text += f" ({warn} with warnings)"
    text += f", {len(by_status['error'])} failed"
    if by_status["skipped"]:
        text += f", {len(by_status['skipped'])} skipped"
    return text


def _overall(by_status: dict[str, list[str]]) -> str:
    good = len(by_status["ok"]) + len(by_status["warn"])
    if by_status["error"]:
        return "partial" if good else "error"
    return "warn" if by_status["warn"] else "ok"


def _crash(jid: int, job_type: str, exc: Exception) -> None:
    """Close a job run whose code raised, so that it does not stay 'running'."""
    summary = f"The job stopped on an unexpected error: {mirror.scrub(repr(exc))}"
    db.finish_job(jid, "error", 0, 1, summary, progress.log_text(jid))
    notify.notify_job(job_type, False, summary)


def run_pull_job() -> None:
    lock = _LOCKS["pull"]
    if not lock.acquire(blocking=False):
        return _skip("pull")
    try:
        jid = db.start_job("pull")
        try:
            problem = storage_guard.mirror_problem()
            if problem:
                refuse(jid, "pull", problem)
                return
            enabled = db.list_repos(enabled_only=True)
            # A gone repository fails every time. Its mirror is kept as it is.
            repos = [r for r in enabled if r["upstream_state"] != "gone"]
            gone = len(enabled) - len(repos)
            progress.start(jid, "pull", "Pull mirrors", len(repos))
            by_status = _sync_repos(jid, repos)

            ok = len(by_status["ok"]) + len(by_status["warn"])
            fail = len(by_status["error"])
            if repos:
                summary = f"Mirrored {_counts_text(by_status)} ({len(repos)} enabled)"
            else:
                summary = "No repository to mirror. Run 'Scan GitHub' first."
            if gone:
                summary += f". Skipped {gone} gone upstream"
            db.finish_job(jid, _overall(by_status), ok, fail, summary,
                          progress.log_text(jid))
            everything = db.list_repos()
            db.add_history_snapshot(jid, len(everything), ok, fail,
                                    sum((r["size_bytes"] or 0) for r in everything))
            notify.notify_job("pull", fail == 0,
                              summary + failed_names_text(by_status["error"]))
        except Exception as exc:
            _crash(jid, "pull", exc)
        finally:
            progress.finish(jid)
    finally:
        lock.release()


class SyncRefused(Exception):
    """A sync of selected repositories cannot start. The text is for the user."""


def prepare_selected_sync(repo_ids: list[int]) -> tuple[int, list[int]]:
    """Reserve the selected repositories and open a job run for them.

    The reservation is made here, in the request, so that a second click is
    refused at once. Pass the result to `run_selected_sync` in the background.

    Returns:
        (job run id, ids of the repositories that were reserved).

    Raises:
        SyncRefused: The full pull job runs, the mirror folder is not good,
            nothing was selected, or every selected repository is in work.
    """
    if is_running("pull"):
        raise SyncRefused("The pull job is running now. It syncs every repository. "
                          "Wait until it is done, then try again.")
    problem = storage_guard.mirror_problem()
    if problem:
        raise SyncRefused(problem)
    repos = db.get_repos(repo_ids)
    if not repos:
        raise SyncRefused("Select one repository or more first.")
    claimed = [r for r in repos if _claim(r["full_name"])]
    if not claimed:
        raise SyncRefused("A sync of this repository is running now." if len(repos) == 1
                          else "The selected repositories are in work now.")
    if len(claimed) == 1:
        label = f"Sync {claimed[0]['full_name']}"
    else:
        label = f"Sync {len(claimed)} selected repositories"
    jid = db.start_job("pull")
    progress.start(jid, "pull", label, len(claimed))
    for r in claimed:
        # Shown as running from this moment, before a worker thread is free.
        progress.begin_item(jid, r["full_name"])
    return jid, [r["id"] for r in claimed]


def run_selected_sync(jid: int, repo_ids: list[int]) -> None:
    """Mirror the repositories that `prepare_selected_sync` reserved."""
    repos = db.get_repos(repo_ids)
    try:
        by_status = _sync_repos(jid, repos, claimed=True)
        ok = len(by_status["ok"]) + len(by_status["warn"])
        fail = len(by_status["error"])
        if len(repos) == 1:
            name = repos[0]["full_name"]
            summary = (f"Sync of {name} failed" if fail else
                       f"Synced {name}" + (" with warnings" if by_status["warn"] else ""))
        else:
            summary = f"Synced {len(repos)} selected repositories: {_counts_text(by_status)}"
        db.finish_job(jid, _overall(by_status), ok, fail, summary, progress.log_text(jid))
    except Exception as exc:
        _crash(jid, "pull", exc)
    finally:
        for r in repos:
            _release(r["full_name"])
        progress.finish(jid)


def abort_selected_sync(jid: int, repo_ids: list[int], reason: str) -> None:
    """Close a prepared sync that could not be started in the background."""
    for r in db.get_repos(repo_ids):
        _release(r["full_name"])
    progress.finish(jid)
    db.finish_job(jid, "error", 0, len(repo_ids), f"The sync did not start: {reason}", "")


# --- verify ---------------------------------------------------------------------

def run_verify_job() -> None:
    lock = _LOCKS["verify"]
    if not lock.acquire(blocking=False):
        return _skip("verify")
    try:
        jid = db.start_job("verify")
        try:
            problem = storage_guard.mirror_problem()
            if problem:
                refuse(jid, "verify", problem)
                return
            # Every mirror on disk, also with the backup off or gone upstream:
            # those are the copies that cannot be made again.
            repos = [r for r in db.list_repos()
                     if (mirror.mirror_dir(r["full_name"]) / "HEAD").is_file()]
            progress.start(jid, "verify", "Verify mirrors", len(repos))
            ok = skipped = 0
            failed: list[str] = []
            last_flush = time.monotonic()
            for r in repos:
                name = r["full_name"]
                if not _claim(name):
                    skipped += 1
                    progress.end_item(jid, name, "skipped",
                                      f"[skipped] {name} — a sync works on it now")
                    continue
                try:
                    progress.begin_item(jid, name)
                    started = time.time()
                    try:
                        good, message = mirror.verify_mirror(name)
                    except Exception as exc:
                        good, message = False, mirror.scrub(f"unexpected error: {exc!r}")
                    status = "ok" if good else "error"
                    db.record_verify(r["id"], good, message)
                    db.add_sync_log(r["id"], jid, started, time.time(), "verify",
                                    status, message, None)
                finally:
                    _release(name)
                if good:
                    ok += 1
                else:
                    failed.append(name)
                progress.end_item(jid, name, status, f"[{status}] {name} — {message}")
                if time.monotonic() - last_flush >= FLUSH_INTERVAL:
                    last_flush = time.monotonic()
                    _flush_progress(jid)

            if repos:
                summary = f"Verified {len(repos) - skipped} mirrors: {ok} ok, {len(failed)} failed"
                if skipped:
                    summary += f", {skipped} skipped"
            else:
                summary = "No mirror on disk to verify. Run 'Pull mirrors' first."
            overall = "ok" if not failed else ("partial" if ok else "error")
            db.finish_job(jid, overall, ok, len(failed), summary, progress.log_text(jid))
            notify.notify_job("verify", not failed, summary + failed_names_text(failed))
        except Exception as exc:
            _crash(jid, "verify", exc)
        finally:
            progress.finish(jid)
    finally:
        lock.release()


# Jobs that write to the mirror folder. rsync must not copy a repository
# while git or a download changes it.
_MIRROR_WRITERS = ("pull", "metadata", "assets")


def _protection_refusal(delete: bool) -> str | None:
    """Return why the protection copy must not run, or None."""
    problem = storage_guard.mirror_problem() or storage_guard.protection_problem()
    if problem:
        return f"{problem} The protection copy did not run."
    # Without "delete" rsync removes nothing, so a mirror folder with few
    # repositories cannot damage the copy.
    return storage_guard.missing_mirrors_problem() if delete else None


def run_protection_job() -> None:
    lock = _LOCKS["protection"]
    if not lock.acquire(blocking=False):
        return _skip("protection")
    try:
        jid = db.start_job("protection")
        try:
            if setup_state.protection_skipped():
                # The owner chose "no protection copy" in the setup. Without this
                # guard, "Run now" would copy to the fallback folder on local disk.
                db.finish_job(jid, "skipped", 0, 0,
                              "The protection copy is turned off. To use it, select a "
                              "protection folder in Settings, Storage.", "")
                return
            delete = db.get_bool("protection_delete")
            problem = _protection_refusal(delete)
            if problem:
                refuse(jid, "protection", problem)
                return
            busy = [JOBS[key].label if key in JOBS else key
                    for key in _MIRROR_WRITERS if is_running(key)]
            if busy:
                names = ", ".join(f"'{label}'" for label in busy)
                db.finish_job(jid, "skipped", 0, 0,
                              f"A job that writes to the mirror folder runs now: {names}. "
                              "The protection copy did not run. Start it again when "
                              "that job is done.", "")
                return
            success, detail = protection.run_protection_copy(delete=delete)
            status = "ok" if success else "error"
            summary = ("Protection copy complete" if success
                       else "Protection copy failed")
            db.finish_job(jid, status, 1 if success else 0, 0 if success else 1,
                          summary, detail)
            notify.notify_job("protection", success, summary)
        except Exception as exc:
            _crash(jid, "protection", exc)
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
                 "Find the repositories on GitHub and add the new ones.",
                 run_scan_job, "0 3 * * *"))
register(JobSpec("pull", "Pull mirrors",
                 "Clone or update the mirror of each repository that has the backup on.",
                 run_pull_job, "30 3 * * *"))
register(JobSpec("protection", "Protection copy",
                 "Copy the mirror folder to the protection folder with rsync.",
                 run_protection_job, "0 5 * * 0"))
register(JobSpec("verify", "Verify mirrors",
                 "Run git fsck on each mirror on disk.",
                 run_verify_job, "0 6 * * 6"))
