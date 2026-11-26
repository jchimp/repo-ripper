"""Template helpers of the sync pages: relative times, durations, status, chart.

Importing this module registers the Jinja filters. The route modules that
render the sync pages import it.
"""
import math
import sqlite3
import time
from collections.abc import Sequence

from markupsafe import Markup, escape

from . import db, jobs, progress
from .web import _fmt_size, _fmt_time, templates


def rel_time(ts: float | None, now: float | None = None) -> str:
    """Return a short relative time, for example '3 h ago'."""
    if not ts:
        return "never"
    delta = (now if now is not None else time.time()) - ts
    if delta < 45:
        return "just now"
    if delta < 90 * 60:
        return f"{max(1, round(delta / 60))} min ago"
    if delta < 36 * 3600:
        return f"{round(delta / 3600)} h ago"
    if delta < 60 * 86400:
        return f"{round(delta / 86400)} d ago"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def ago(ts: float | None) -> Markup:
    """Relative time as HTML, with the absolute time in the `title`."""
    if not ts:
        return Markup('<span class="muted">never</span>')
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
    return Markup('<time datetime="{}" title="{}">{}</time>').format(
        iso, _fmt_time(ts), rel_time(ts))


def fmt_duration(seconds: float | None) -> str:
    """Return a duration as '8 s', '2 min 5 s' or '1 h 3 min'."""
    if seconds is None:
        return "—"
    total = int(round(seconds))
    if total < 1:
        return "< 1 s"
    if total < 60:
        return f"{total} s"
    if total < 3600:
        return f"{total // 60} min {total % 60} s"
    return f"{total // 3600} h {total % 3600 // 60} min"


def repo_status(row: sqlite3.Row) -> str:
    """Return the status to show for a repository row.

    One of 'running', 'ok', 'warn', 'error' or 'pending' (never synced).
    """
    if row["full_name"] in progress.running_names():
        return "running"
    return row["last_status"] or "pending"


templates.env.filters["ago"] = ago
templates.env.filters["fmt_duration"] = fmt_duration
templates.env.filters["repo_status"] = repo_status


# --- mirror size chart --------------------------------------------------------

_CHART_H = 220          # px, the whole SVG
_PLOT_TOP = 16          # px above the plot, room for the top label
_PLOT_H = 168           # px, the plot
_MAX_DOTS = 45          # with more points, only the last one gets a dot

_UNITS = ("B", "KB", "MB", "GB", "TB")


def _nice_axis(max_bytes: int) -> tuple[float, str, float]:
    """Return (axis top in bytes, unit, step in bytes) for 2-4 round steps."""
    unit_index = 0
    value = float(max(max_bytes, 1))
    while value >= 1024 and unit_index < len(_UNITS) - 1:
        value /= 1024
        unit_index += 1
    magnitude = 10 ** math.floor(math.log10(value)) if value > 0 else 1
    step = magnitude
    for factor in (0.25, 0.5, 1, 2.5, 5, 10):
        step = factor * magnitude
        if value / step <= 4:
            break
    top = math.ceil(value / step) * step
    scale = 1024 ** unit_index
    return top * scale, _UNITS[unit_index], step * scale


def _axis_label(value_bytes: float, unit: str) -> str:
    number = value_bytes / (1024 ** _UNITS.index(unit))
    return f"{number:g} {unit}"


def size_chart(points: Sequence[tuple[float, int]]) -> Markup | None:
    """Render the mirror size over time as an inline SVG.

    The SVG has no viewBox: horizontal positions are percentages and vertical
    positions are pixels, so the text keeps its size at every page width.
    Colours come from CSS classes only.

    Args:
        points: (unix time, total bytes), oldest first.

    Returns:
        The SVG, or None when there are fewer than 2 points (the caller shows
        the empty state).
    """
    if len(points) < 2:
        return None
    t0, t1 = points[0][0], points[-1][0]
    span = max(t1 - t0, 1.0)
    top, unit, step = _nice_axis(max(size for _, size in points))

    def x_of(ts: float) -> float:
        return (ts - t0) / span * 100

    def y_of(size: float) -> float:
        return (1 - size / top) * 100       # percent of the plot height

    def day(ts: float) -> str:
        # Points of one or two days: the date alone does not tell them apart.
        fmt = "%Y-%m-%d" if span >= 2 * 86400 else "%Y-%m-%d %H:%M"
        return time.strftime(fmt, time.localtime(ts))

    label = (f"Mirror size over time. {len(points)} pull jobs from {day(t0)} to {day(t1)}. "
             f"First {_fmt_size(points[0][1])}, latest {_fmt_size(points[-1][1])}.")
    out: list[str] = [
        Markup('<svg class="chart" width="100%" height="{}" role="img" aria-label="{}">'
               '<title>{}</title>').format(_CHART_H, label, label)
    ]

    # Grid lines with the value label on the line, at the left.
    out.append('<g class="chart-grid">')
    value = 0.0
    while value <= top + step / 2:
        y = _PLOT_TOP + _PLOT_H * y_of(value) / 100
        css = "chart-base" if value == 0 else "chart-rule"
        out.append(f'<line class="{css}" x1="0" x2="100%" y1="{y:.1f}" y2="{y:.1f}"/>')
        if value > 0:
            out.append(f'<text class="chart-label" x="0" y="{y - 5:.1f}">'
                       f'{escape(_axis_label(value, unit))}</text>')
        value += step
    out.append('</g>')

    # The plot is a nested SVG that stretches: x 0-1000, y 0-100.
    coords = [(x_of(ts) * 10, y_of(size)) for ts, size in points]
    line = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.2f}" for i, (x, y) in enumerate(coords))
    area = f"{line} L{coords[-1][0]:.1f} 100 L{coords[0][0]:.1f} 100 Z"
    out.append(f'<svg x="0" y="{_PLOT_TOP}" width="100%" height="{_PLOT_H}" '
               'viewBox="0 0 1000 100" preserveAspectRatio="none" overflow="visible">'
               f'<path class="chart-area" d="{area}"/>'
               f'<path class="chart-line" d="{line}"/></svg>')

    # Points: a larger invisible circle makes the hover target.
    out.append('<g class="chart-points">')
    shown = points if len(points) <= _MAX_DOTS else points[-1:]
    for ts, size in shown:
        cy = _PLOT_TOP + _PLOT_H * y_of(size) / 100
        tip = f"{_fmt_time(ts)}: {_fmt_size(size)}"
        out.append(f'<g class="chart-pt"><title>{escape(tip)}</title>'
                   f'<circle class="chart-hit" cx="{x_of(ts):.2f}%" cy="{cy:.1f}" r="10"/>'
                   f'<circle class="chart-dot" cx="{x_of(ts):.2f}%" cy="{cy:.1f}" r="4"/></g>')
    out.append('</g>')

    # Time axis: first and last date, and the middle when there is room.
    y = _CHART_H - 8
    out.append('<g class="chart-axis">')
    out.append(f'<text class="chart-label" x="0" y="{y}">{day(t0)}</text>')
    if len(points) > 2 and span >= 4 * 86400:
        out.append(f'<text class="chart-label" x="50%" y="{y}" text-anchor="middle">'
                   f'{day(t0 + span / 2)}</text>')
    out.append(f'<text class="chart-label" x="100%" y="{y}" text-anchor="end">{day(t1)}</text>')
    out.append('</g></svg>')
    return Markup("".join(str(part) for part in out))


# --- running jobs --------------------------------------------------------------

def live_jobs() -> list[dict]:
    """Return what the live panels show: one dict for each running job run.

    Keys: `job` (the job_runs row), `label`, `elapsed` (seconds) and `snap`
    (the progress snapshot, or None for a job that reports no progress).
    """
    out: list[dict] = []
    now = time.time()
    for job in db.running_jobs():
        snap = progress.snapshot(job["id"])
        spec = jobs.JOBS.get(job["job_type"])
        label = snap["label"] if snap else (spec.label if spec else job["job_type"])
        out.append({"job": job, "label": label, "snap": snap,
                    "elapsed": now - job["started_at"]})
    return out
