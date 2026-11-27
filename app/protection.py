"""Protection copy: rsync the primary mirror share to a second share.

`--delete` keeps the copy an exact replica (drops repos removed upstream). It
also means a corrupted or truncated primary propagates on the next run, so it's
exposed as a setting, and it is off by default. Off makes the copy append-only
at the cost of letting deleted repos linger.

The caller (`jobs.run_protection_job`) examines both folders before it calls
this module. This module does not make the protection folder: a missing folder
is a share that is not mounted.
"""
import subprocess

from . import runtime, storage_guard

TIMEOUT = 6 * 3600


def run_protection_copy(delete: bool) -> tuple[bool, str]:
    """Run rsync from the mirror folder to the protection folder.

    Args:
        delete: True adds `--delete`: files that are not in the mirror folder
            are removed from the protection folder.

    Returns:
        (success, text for the job log). It does not raise when rsync cannot
        start or runs too long.
    """
    # Trailing slash: rsync copies the contents of src, not the folder itself.
    src = runtime.mirror_root().as_posix().rstrip("/") + "/"
    dst = runtime.protection_root().as_posix().rstrip("/") + "/"

    # Each folder keeps its own marker file: the marker of the protection
    # folder must say that THAT folder was selected, not that a copy ran.
    cmd = ["rsync", "-a", "--stats", "--human-readable",
           f"--exclude=/{storage_guard.MARKER_NAME}"]
    if delete:
        cmd.append("--delete")
    cmd += [src, dst]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, (f"rsync did not complete in {TIMEOUT // 3600} hours and was stopped. "
                       "The next run continues the copy.")
    except OSError as exc:
        # rsync is not installed, or the program cannot be started.
        return False, f"rsync did not start: {exc}"
    if res.returncode != 0:
        return False, res.stderr.strip()[-1500:] or f"rsync exit {res.returncode}"
    return True, res.stdout.strip()[-1500:]
