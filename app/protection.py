"""Protection copy: rsync the primary mirror share to a second share.

`--delete` keeps the copy an exact replica (drops repos removed upstream). It
also means a corrupted or truncated primary propagates on the next run, so it's
exposed as a setting. Turn it off to make the copy append-only at the cost of
letting deleted repos linger.
"""
import subprocess
from pathlib import Path

from .config import get_settings


def run_protection_copy(delete: bool) -> tuple[bool, str]:
    s = get_settings()
    src = s.mirror_root.rstrip("/") + "/"
    dst = s.protection_root.rstrip("/") + "/"
    Path(s.protection_root).mkdir(parents=True, exist_ok=True)

    cmd = ["rsync", "-a", "--stats", "--human-readable"]
    if delete:
        cmd.append("--delete")
    cmd += [src, dst]

    res = subprocess.run(cmd, capture_output=True, text=True, timeout=6 * 3600)
    if res.returncode != 0:
        return False, res.stderr.strip()[-1500:] or f"rsync exit {res.returncode}"
    return True, res.stdout.strip()[-1500:]
