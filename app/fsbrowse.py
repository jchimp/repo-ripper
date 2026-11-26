"""Server-side folder browser for the path picker.

The browser shows folders that are mounted into the container, and only below
the allowed roots (`BROWSE_ROOTS`). The security rule is in one place,
`safe_resolve`: every path from the browser is resolved first (this removes
`..` and follows symlinks), and is then refused unless the result is inside an
allowed root. Files are never listed.
"""
import errno
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import get_settings

UID_FIX = (
    "The container runs as uid 1000. Make the share writable by uid 1000: on the "
    "NAS or the Docker host, change the owner of the folder to 1000:1000, or mount "
    "the share with uid=1000."
)
_MAX_NAME = 255


class BrowseError(Exception):
    """A request that the browser refuses. The message is safe to show."""


@dataclass(frozen=True)
class Crumb:
    """One clickable part of the current path."""
    label: str
    path: str


@dataclass
class Listing:
    """What the picker shows for one folder. `path` is None for the root list."""
    path: str | None
    crumbs: list[Crumb] = field(default_factory=list)
    parent: str | None = None          # None = "Up" goes to the root list
    dirs: list[Crumb] = field(default_factory=list)
    missing_roots: list[str] = field(default_factory=list)


@dataclass
class FolderCheck:
    """Space and write-test result for a selected folder."""
    path: str
    free: int | None = None
    total: int | None = None
    writable: bool = False
    problem: str = ""                  # plain words, empty when all is OK


def allowed_roots() -> list[Path]:
    """Return the resolved allowed roots from `BROWSE_ROOTS`."""
    roots: list[Path] = []
    for raw in get_settings().browse_roots.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            root = Path(raw).resolve()
        except (OSError, ValueError):
            continue
        if root not in roots:
            roots.append(root)
    return roots


def _root_of(path: Path) -> Path | None:
    """Return the allowed root that contains `path`, or None."""
    for root in allowed_roots():
        if path == root or path.is_relative_to(root):
            return root
    return None


def safe_resolve(raw: str) -> Path:
    """Resolve a path from the browser and make sure it is inside a root.

    Args:
        raw: The path as the browser sent it.

    Returns:
        The resolved absolute path. It can be a path that does not exist.

    Raises:
        BrowseError: The path is empty, not absolute, not valid, or it is
            outside the allowed roots after `..` and symlinks are resolved.
    """
    raw = (raw or "").strip()
    if not raw:
        raise BrowseError("Enter a folder path.")
    try:
        candidate = Path(raw)
        if not candidate.is_absolute():
            raise BrowseError("Enter a full path that starts at the root, for example /mnt/nas.")
        resolved = candidate.resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        # ValueError: NUL byte. RuntimeError: symlink loop on old Pythons.
        raise BrowseError("This path is not valid.") from exc
    if _root_of(resolved) is None:
        roots = ", ".join(str(r) for r in allowed_roots()) or "(none)"
        raise BrowseError(
            f"This folder is outside the folders that the app can browse ({roots}). "
            "Mount the folder into the container below one of them."
        )
    return resolved


def validate_name(name: str) -> str:
    """Check the name of a new folder: one path part, no separators, no `..`.

    Raises:
        BrowseError: The name is not acceptable.
    """
    name = (name or "").strip()
    if not name:
        raise BrowseError("Enter a name for the new folder.")
    if name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise BrowseError('The folder name must not contain "/", "\\" or "..".')
    if len(name) > _MAX_NAME or any(ord(c) < 32 for c in name):
        raise BrowseError("The folder name is too long or has control characters.")
    return name


def roots_listing() -> Listing:
    """Return the top level of the picker: the allowed roots."""
    listing = Listing(path=None)
    for root in allowed_roots():
        if root.is_dir():
            listing.dirs.append(Crumb(str(root), str(root)))
        else:
            listing.missing_roots.append(str(root))
    return listing


def list_dir(raw: str) -> Listing:
    """List the sub-folders of one folder.

    Args:
        raw: The folder path as the browser sent it.

    Returns:
        The listing: breadcrumb parts, parent, and sub-folders. Files are not
        included. Links that point outside the allowed roots are not included,
        because the browser cannot open them.

    Raises:
        BrowseError: The path is refused, is not a folder, or cannot be read.
    """
    path = safe_resolve(raw)
    if not path.is_dir():
        raise BrowseError(
            f"The folder {path} does not exist. Open the parent folder and use \"New folder\"."
        )
    root = _root_of(path)
    assert root is not None  # safe_resolve guarantees it

    crumbs = [Crumb(str(root), str(root))]
    walk = root
    for part in path.relative_to(root).parts:
        walk = walk / part
        crumbs.append(Crumb(part, str(walk)))

    dirs: list[Crumb] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                    target = Path(entry.path).resolve()
                except OSError:
                    continue  # broken link or unreadable entry: cannot be opened
                if _root_of(target) is None:
                    continue
                dirs.append(Crumb(entry.name, str(Path(entry.path))))
    except PermissionError as exc:
        raise BrowseError(f"The app has no permission to read {path}. {UID_FIX}") from exc
    except OSError as exc:
        raise BrowseError(f"The folder {path} cannot be read: {exc.strerror or exc}.") from exc
    dirs.sort(key=lambda c: c.label.lower())

    parent = None if path == root else str(path.parent)
    return Listing(path=str(path), crumbs=crumbs, parent=parent, dirs=dirs)


def nearest_listing(raw: str) -> Listing:
    """List the nearest existing parent of a path that cannot be listed.

    Used after `list_dir` refused a path, so that the picker still shows a
    useful place. Falls back to the root list.
    """
    try:
        path = safe_resolve(raw)
    except BrowseError:
        return roots_listing()
    root = _root_of(path)
    while root is not None and path != root and not path.is_dir():
        path = path.parent
    try:
        return list_dir(str(path))
    except BrowseError:
        return roots_listing()


FIELDS: dict[str, tuple[str, str]] = {
    "mirror_root": (
        "Mirror folder",
        "The folder that holds the mirrors. Each repository becomes <owner>/<repo>.git.",
    ),
    "protection_root": (
        "Protection folder",
        "The folder that receives the protection copy. Use a different disk or share.",
    ),
}


def folder_field(name: str, value: str | Path, *, check: FolderCheck | None = None,
                 error: str = "", warning: str = "") -> dict:
    """Template values for one folder field (`_folder_field.html`)."""
    label, hint = FIELDS[name]
    return {"field": name, "label": label, "hint": hint, "value": str(value),
            "check": check, "error": error, "warning": warning}


def make_dir(parent_raw: str, name: str) -> Path:
    """Create one new folder inside an existing folder.

    Raises:
        BrowseError: The parent or the name is refused, or the folder cannot
            be made.
    """
    parent = safe_resolve(parent_raw)
    if not parent.is_dir():
        raise BrowseError(f"The folder {parent} does not exist.")
    target = safe_resolve(str(parent / validate_name(name)))
    try:
        target.mkdir()
    except FileExistsError as exc:
        raise BrowseError(
            f"A folder or file with the name {target.name} is already there."
        ) from exc
    except PermissionError as exc:
        raise BrowseError(
            f"The app has no permission to make a folder in {parent}. {UID_FIX}"
        ) from exc
    except OSError as exc:
        raise BrowseError(f"The folder cannot be made: {exc.strerror or exc}.") from exc
    return target


def _write_problem(exc: OSError) -> str:
    if exc.errno in (errno.EACCES, errno.EPERM):
        return f"The app has no permission to write in this folder. {UID_FIX}"
    if exc.errno == errno.EROFS:
        return ("The folder is mounted read-only. Remove \":ro\" from the volume in "
                "docker-compose.yml, or mount the share read-write on the Docker host.")
    if exc.errno == errno.ENOSPC:
        return "The disk is full. Make space, then check again."
    return f"The write test failed: {exc.strerror or exc}. {UID_FIX}"


def check_folder(path: Path) -> FolderCheck:
    """Measure free space and do a write test (make and delete a small file).

    Args:
        path: A folder that `safe_resolve` already accepted.

    Returns:
        The result. `problem` says in plain words what is wrong.
    """
    check = FolderCheck(path=str(path))
    if not path.is_dir():
        check.problem = f"The folder {path} does not exist."
        return check
    try:
        usage = shutil.disk_usage(path)
        check.free, check.total = usage.free, usage.total
    except OSError:
        pass  # space is information only; the write test below decides
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".repo-ripper-write-test-", dir=path)
        try:
            os.write(fd, b"repo-ripper write test\n")
        finally:
            os.close(fd)
            os.unlink(tmp_name)
    except OSError as exc:
        check.problem = _write_problem(exc)
        return check
    check.writable = True
    return check


def prepare_folder(raw: str) -> tuple[Path, FolderCheck]:
    """Validate a folder that the user selected for storage.

    A folder that does not exist yet is made, because the sync would make it
    in any case. The folder must pass the write test.

    Raises:
        BrowseError: The path is refused, cannot be made, or is not writable.
    """
    path = safe_resolve(raw)
    if path.exists() and not path.is_dir():
        raise BrowseError(f"{path} is a file, not a folder.")
    if not path.exists():
        try:
            path.mkdir(parents=True)
        except PermissionError as exc:
            raise BrowseError(f"The app has no permission to make {path}. {UID_FIX}") from exc
        except OSError as exc:
            raise BrowseError(f"The folder {path} cannot be made: {exc.strerror or exc}.") from exc
    check = check_folder(path)
    if not check.writable:
        raise BrowseError(check.problem)
    return path, check


def overlap_warning(mirror: str | Path | None, protection: str | Path | None) -> str:
    """Return a warning when the two folders are the same or nested, else ''."""
    if not mirror or not protection:
        return ""
    try:
        a, b = Path(mirror).resolve(), Path(protection).resolve()
    except (OSError, ValueError):
        return ""
    if a == b:
        return ("The mirror folder and the protection folder are the same folder. "
                "The protection copy then protects nothing. Use a folder on a different disk.")
    if a.is_relative_to(b) or b.is_relative_to(a):
        return ("One of the two folders is inside the other. The protection copy then "
                "copies into its own source. Use two separate folders, on different disks "
                "if possible.")
    return ""
