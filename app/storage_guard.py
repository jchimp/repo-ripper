"""Checks that a storage folder is the folder that the user selected.

The mirror folder and the protection folder are paths below a mount point. If
the NAS share is not mounted, the path is an empty folder on the disk of the
Docker host, and a job would write there with no error. To see the difference,
the app writes a marker file into a folder when the user selects it. A job
that writes examines the marker first.

Jobs call the `*_problem` functions. Each one returns a text for the user, or
None when the folder is good.
"""
import logging
import os
from pathlib import Path

from . import db, runtime

log = logging.getLogger("repo-ripper")

MARKER_NAME = ".repo-ripper-root"
ADOPTED_KEY = "storage_marker_adopted"

_MARKER_TEXT = (
    "repo-ripper storage folder.\n"
    "\n"
    "repo-ripper writes this file when you select the folder in the setup or in\n"
    "Settings, Storage. Each job examines this file before it writes. If the\n"
    "file is not there, the job stops, because the NAS share is probably not\n"
    "mounted. Do not delete this file.\n"
)


def marker_path(folder: Path) -> Path:
    """Return the path of the marker file of a storage folder."""
    return folder / MARKER_NAME


def has_marker(folder: Path) -> bool:
    """Return True when the marker file is in the folder."""
    try:
        return marker_path(folder).is_file()
    except OSError:
        # For example a stale SMB mount: the folder is not usable.
        return False


def write_marker(folder: Path) -> None:
    """Write the marker file into a storage folder that exists.

    Args:
        folder: The mirror folder or the protection folder.

    Raises:
        OSError: The file cannot be written.
    """
    target = marker_path(folder)
    # A temporary name and then a rename: an SMB share can show a file that
    # is only partly written.
    tmp = folder / f"{MARKER_NAME}.tmp"
    tmp.write_text(_MARKER_TEXT, encoding="utf-8")
    os.replace(tmp, target)


def _problem(label: str, folder: Path) -> str | None:
    if has_marker(folder):
        return None
    return (f"The {label} folder {folder} does not have the file {MARKER_NAME}. "
            "The NAS share is probably not mounted. If you moved the folder, "
            "select it again in Settings, Storage.")


def mirror_problem() -> str | None:
    """Return why the mirror folder must not be used, or None when it is good."""
    return _problem("mirror", runtime.mirror_root())


def protection_problem() -> str | None:
    """Return why the protection folder must not be used, or None when it is good."""
    return _problem("protection", runtime.protection_root())


def missing_mirrors_problem() -> str | None:
    """Return a problem text when the mirror folder holds far fewer mirrors than expected.

    The database knows which repositories had a good sync. If fewer than half
    of them are on disk, the mirror folder is probably a new or wrong disk. A
    protection copy with "delete" on would then remove the missing ones from
    the protection folder, which is the only copy that remains.
    """
    root = runtime.mirror_root()
    expected = on_disk = 0
    for r in db.list_repos():
        if r["last_ok_at"] is None:
            continue
        expected += 1
        if (root / f"{r['full_name']}.git" / "HEAD").is_file():
            on_disk += 1
    if expected == 0 or on_disk * 2 >= expected:
        return None
    return (f"The mirror folder {root} has {on_disk} of the {expected} repositories that had "
            f"a good sync. With \"delete\" on, the protection copy would remove the "
            f"{expected - on_disk} missing repositories from the protection folder. "
            "The copy did not run. Examine the mirror folder. To copy and remove "
            "nothing, turn off \"delete\" in Settings.")


def _holds_mirror(root: Path) -> bool:
    """True when the folder has at least one `<owner>/<name>.git/HEAD`."""
    for owner in root.iterdir():
        if not owner.is_dir():
            continue
        for entry in owner.iterdir():
            if entry.name.endswith(".git") and (entry / "HEAD").is_file():
                return True
    return False


def adopt_existing_folders() -> None:
    """Give the marker file to the folders of an install from before the marker.

    Runs one time, at the first start of a version that has the marker. A
    folder gets the marker only when it already holds data: an empty or
    missing folder can be a share that is not mounted. For such a folder the
    jobs report the problem, and the user selects the folder again in
    Settings, which writes the marker. This function never raises: a storage
    problem must not stop the start of the app.
    """
    if db.get_setting(ADOPTED_KEY) == "1":
        return
    done = True
    checks = [("mirror", runtime.mirror_root(), _holds_mirror)]
    # No protection copy: the fallback folder is not in use, leave it alone.
    if not db.get_bool("protection_skip", False):
        checks.append(("protection", runtime.protection_root(),
                       lambda folder: any(folder.iterdir())))
    for label, folder, holds_data in checks:
        try:
            if folder.is_dir() and not has_marker(folder) and holds_data(folder):
                write_marker(folder)
                log.info("Wrote %s into the %s folder %s.", MARKER_NAME, label, folder)
        except OSError as exc:
            # Try again at the next start; until then the jobs report the problem.
            done = False
            log.warning("Cannot write %s into the %s folder %s: %s",
                        MARKER_NAME, label, folder, exc)
    if done:
        db.set_setting(ADOPTED_KEY, "1")
