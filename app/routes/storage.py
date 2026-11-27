"""Storage page: folders, disk space, sizes, protection copy, size history.

Nothing here walks the NAS. The sizes come from the database (the jobs record
them); a page load only asks the file system for the marker file and the disk
space of the two folders.
"""
import shutil
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import db, jobs, runtime, scheduler, setup_state, storage_guard, viewhelpers
from ..web import render

router = APIRouter()

PROTECTION_JOB = "protection"
LARGEST = 10        # rows in the "largest repositories" list
RECENT_RUNS = 5     # protection copy runs in the list


@dataclass(frozen=True)
class Folder:
    """What the page shows about one storage folder."""

    label: str
    path: Path
    marker_ok: bool
    total: int | None = None
    used: int | None = None
    free: int | None = None

    @property
    def used_percent(self) -> int:
        return round(self.used * 100 / self.total) if self.total and self.used is not None else 0


def _folder(label: str, path: Path) -> Folder:
    marker_ok = storage_guard.has_marker(path)
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        # The folder does not exist or the share does not answer.
        return Folder(label, path, marker_ok)
    return Folder(label, path, marker_ok, usage.total, usage.used, usage.free)


def _largest(repos: list) -> list[dict]:
    rows = []
    for r in repos:
        parts = (r["size_bytes"], r["meta_bytes"], r["assets_bytes"])
        rows.append({"repo": r, "total": sum(p or 0 for p in parts)})
    rows.sort(key=lambda row: (-row["total"], row["repo"]["full_name"].lower()))
    return [row for row in rows[:LARGEST] if row["total"]]


@router.get("/storage", response_class=HTMLResponse)
def storage_page(request: Request):
    repos = db.list_repos()
    sizes = db.size_totals(repos)
    history = db.history_snapshots()
    runs, _total = db.query_jobs(job_type=PROTECTION_JOB, limit=RECENT_RUNS)
    chart = viewhelpers.size_chart(
        [h["recorded_at"] for h in history],
        [("Code", "code", [h["total_bytes"] for h in history]),
         ("Metadata", "meta", [h["meta_bytes"] for h in history]),
         ("Release files", "assets", [h["assets_bytes"] for h in history])])
    return render(request, "storage.html", {
        "folders": [_folder("Mirror folder", runtime.mirror_root()),
                    _folder("Protection folder", runtime.protection_root())],
        "protection_skipped": setup_state.protection_skipped(),
        "sizes": sizes,
        "size_sum": sum(v or 0 for v in sizes.values()),
        # Only repositories that the metadata job captures get a size.
        "meta_unmeasured": sum(1 for r in repos if r["meta_bytes"] is None
                               and r["enabled"] and r["metadata_enabled"]),
        "repo_count": len(repos),
        "largest": _largest(repos),
        "protection_runs": runs,
        "protection_next": scheduler.next_runs().get(PROTECTION_JOB),
        "protection_running": jobs.is_running(PROTECTION_JOB),
        "history": history,
        "chart": chart,
    })
