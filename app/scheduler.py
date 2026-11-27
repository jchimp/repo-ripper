"""APScheduler wiring.

Blocking git/rsync work runs in a thread pool so it never stalls the event loop.
Schedules are stored in the DB as cron strings and can be re-applied live from
the settings page.
"""
import logging
from collections.abc import Callable
from datetime import datetime

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_SUBMITTED,
    JobEvent,
    JobExecutionEvent,
)
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db, jobs
from .config import get_settings

scheduler = BackgroundScheduler(
    executors={"default": ThreadPoolExecutor(4)},
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
    timezone=get_settings().tz,
)

log = logging.getLogger("repo-ripper")

CRON_PROBLEM = "This is not a valid schedule. Use five fields: minute hour day month weekday."


def cron_problem(text: str) -> str | None:
    """Check one schedule string.

    Args:
        text: The cron string. An empty string means "manual only" and is valid.

    Returns:
        None if the scheduler can use the string, or a message for the user.
    """
    if not text.strip():
        return None
    try:
        CronTrigger.from_crontab(text)
    except ValueError:
        return CRON_PROBLEM
    return None


def _log_job_event(event: JobEvent) -> None:
    """Write one process log line for the start and the end of each scheduler job."""
    if event.code == EVENT_JOB_SUBMITTED:
        log.info("job %s started", event.job_id)
    elif isinstance(event, JobExecutionEvent) and event.exception is not None:
        # Only the type: an exception text can hold a URL or a path.
        log.error("job %s raised %s", event.job_id, type(event.exception).__name__)
    else:
        log.info("job %s finished", event.job_id)


scheduler.add_listener(
    _log_job_event, EVENT_JOB_SUBMITTED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)


def start() -> None:
    if not scheduler.running:
        scheduler.start()
    configure_jobs()


def configure_jobs() -> None:
    """Apply the current cron settings, replacing existing scheduled jobs."""
    tz = get_settings().tz
    for spec in jobs.JOBS.values():
        cron = db.get_setting(spec.schedule_key)
        if cron is None:
            # A job added after first boot has no stored schedule yet.
            cron = spec.default_cron
            db.set_setting(spec.schedule_key, cron)
        elif cron_problem(cron):
            # A job without a trigger never runs again, and nothing shows it.
            # Use the default, and store it so that Settings shows what is in effect.
            log.warning(
                "The stored schedule %r of the job %s is not valid. "
                "The default %r is used and stored.", cron, spec.key, spec.default_cron)
            cron = spec.default_cron
            db.set_setting(spec.schedule_key, cron)
        if not cron.strip():
            # Empty schedule = manual only.
            if scheduler.get_job(spec.key):
                scheduler.remove_job(spec.key)
            continue
        trigger = CronTrigger.from_crontab(cron, timezone=tz)
        scheduler.add_job(spec.run, trigger, id=spec.key, replace_existing=True)


def trigger_now(job_type: str) -> None:
    """Fire a job immediately, out of band from its schedule."""
    # An aware time: APScheduler reads a naive time in the scheduler time zone,
    # and drops the job as missed when the process time zone is a different one.
    now = datetime.now(scheduler.timezone)
    scheduler.add_job(
        jobs.JOBS[job_type].run, "date", run_date=now,
        id=f"manual-{job_type}-{now.timestamp()}",
    )


def run_in_background(func: Callable[..., None], *args: object) -> None:
    """Run a function one time, now, in the scheduler's thread pool."""
    now = datetime.now(scheduler.timezone)  # aware, see trigger_now
    scheduler.add_job(
        func, "date", run_date=now, args=args,
        id=f"once-{func.__name__}-{now.timestamp()}",
    )


def next_runs() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for job_type in jobs.JOBS:
        job = scheduler.get_job(job_type)
        out[job_type] = (
            job.next_run_time.strftime("%Y-%m-%d %H:%M %Z")
            if job and job.next_run_time else None
        )
    return out


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
