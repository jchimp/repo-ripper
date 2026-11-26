"""APScheduler wiring.

Blocking git/rsync work runs in a thread pool so it never stalls the event loop.
Schedules are stored in the DB as cron strings and can be re-applied live from
the settings page.
"""
from collections.abc import Callable
from datetime import datetime

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
        if not cron.strip():
            # Empty schedule = manual only.
            if scheduler.get_job(spec.key):
                scheduler.remove_job(spec.key)
            continue
        try:
            trigger = CronTrigger.from_crontab(cron, timezone=tz)
        except ValueError:
            continue  # bad cron string in DB; leave the prior schedule intact
        scheduler.add_job(spec.run, trigger, id=spec.key, replace_existing=True)


def trigger_now(job_type: str) -> None:
    """Fire a job immediately, out of band from its schedule."""
    scheduler.add_job(
        jobs.JOBS[job_type].run, "date", run_date=datetime.now(),
        id=f"manual-{job_type}-{datetime.now().timestamp()}",
    )


def run_in_background(func: Callable[..., None], *args: object) -> None:
    """Run a function one time, now, in the scheduler's thread pool."""
    scheduler.add_job(
        func, "date", run_date=datetime.now(), args=args,
        id=f"once-{func.__name__}-{datetime.now().timestamp()}",
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
