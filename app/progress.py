"""Live progress of running jobs, kept in memory.

Worker threads report here; the web handlers read a snapshot every 2 seconds.
The registry is lost on a restart, which is correct: a restart also stops the
job (see `db.mark_interrupted_jobs`). The job log itself is written to
`job_runs.detail` at intervals, so it survives.
"""
import threading
import time
from dataclasses import dataclass, field

_LOCK = threading.Lock()


@dataclass
class _JobProgress:
    job_id: int
    job_type: str
    label: str
    total: int
    started_at: float
    done: int = 0
    ok: int = 0
    warn: int = 0
    failed: int = 0
    skipped: int = 0
    running: dict[str, float] = field(default_factory=dict)  # name -> start time
    lines: list[str] = field(default_factory=list)


_JOBS: dict[int, _JobProgress] = {}


def start(job_id: int, job_type: str, label: str, total: int) -> None:
    """Register a job run that will report progress."""
    with _LOCK:
        _JOBS[job_id] = _JobProgress(job_id, job_type, label, total, time.time())


def begin_item(job_id: int, name: str) -> None:
    """Mark one repository as in work."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            job.running[name] = time.time()


def end_item(job_id: int, name: str, status: str, line: str) -> None:
    """Mark one repository as done and add its log line.

    Args:
        job_id: The job run id.
        name: Repository full name.
        status: 'ok', 'warn', 'error' or 'skipped'.
        line: The log line for this repository.
    """
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job.running.pop(name, None)
        job.done += 1
        if status == "ok":
            job.ok += 1
        elif status == "warn":
            job.warn += 1
        elif status == "skipped":
            job.skipped += 1
        else:
            job.failed += 1
        job.lines.append(line)


def log(job_id: int, line: str) -> None:
    """Add a log line that does not belong to one repository."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            job.lines.append(line)


def finish(job_id: int) -> None:
    """Remove a job run from the registry. Call it after the last DB write."""
    with _LOCK:
        _JOBS.pop(job_id, None)


def snapshot(job_id: int) -> dict | None:
    """Return a copy of the progress of one job run, or None if not tracked."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        return _copy(job)


def active() -> list[dict]:
    """Return a snapshot of every tracked job run, oldest first."""
    with _LOCK:
        return [_copy(j) for j in sorted(_JOBS.values(), key=lambda j: j.started_at)]


def running_names() -> set[str]:
    """Return the full names of the repositories that are in work now."""
    with _LOCK:
        return {name for j in _JOBS.values() for name in j.running}


def log_text(job_id: int) -> str:
    """Return the log so far as one string."""
    with _LOCK:
        job = _JOBS.get(job_id)
        return "\n".join(job.lines) if job else ""


def _copy(job: _JobProgress) -> dict:
    percent = int(job.done * 100 / job.total) if job.total else 0
    return {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "label": job.label,
        "total": job.total,
        "done": job.done,
        "ok": job.ok,
        "warn": job.warn,
        "failed": job.failed,
        "skipped": job.skipped,
        "percent": percent,
        "started_at": job.started_at,
        "running": sorted(job.running, key=lambda n: job.running[n]),
        "lines": list(job.lines),
    }
