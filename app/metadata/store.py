"""On-disk metadata store: layout, atomic writes, and the read API.

Layout for the repository `owner/name` (base: `runtime.mirror_root()`):

    <owner>/<name>.meta/
        README.txt            what this folder is, format version
        repo.json             the GitHub repository object
        labels.json           list
        milestones.json       list (open and closed)
        releases.json         list, each with its assets metadata
        issues/<number>.json  {"issue": {...}, "comments": [...], "fetched_at": "..."}
        pulls/<number>.json   {"pull": {...}, "comments": [...], "review_comments": [...],
                               "reviews": [...], "commits": [...], "fetched_at": "..."}
        index.json            {"format_version": 1, "items": [<index entry>, ...]}
        state.json            sync cursor, counters, status of the last run

The read functions do not use the network or the database. They validate
`full_name` and `number`, so a caller cannot read a path outside the mirror
root. An invalid value raises `ValueError`. Data that is not there gives an
empty result (`None`, `[]` or zero counts), not an exception.
"""
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .. import runtime

FORMAT_VERSION = 1

KIND_ISSUE = "issue"
KIND_PULL = "pull"
_KIND_DIRS = {KIND_ISSUE: "issues", KIND_PULL: "pulls"}

# GitHub rules: owner = letters, digits, hyphen. Name = letters, digits, dot,
# underscore, hyphen. The first character class also keeps out a leading dot.
_FULL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}/[A-Za-z0-9._-]{1,100}")
_TMP_SUFFIX = ".tmp"

_README = f"""repo-ripper metadata folder (format version {FORMAT_VERSION}).

This folder holds the GitHub metadata of one repository as plain JSON files:
repo.json, labels.json, milestones.json, releases.json, one file for each issue
(issues/<number>.json) and one file for each pull request (pulls/<number>.json).
The objects are stored as the GitHub REST API returns them. Text bodies are raw
Markdown. index.json is a compact list of all issues and pull requests.
state.json holds the sync position. repo-ripper writes these files; do not edit
them. The git data is in the mirror folder with the same name and ".git".
"""


# --- paths and validation ---------------------------------------------------

def validate_full_name(full_name: str) -> str:
    """Return `full_name` when it is a safe `owner/name` value.

    Raises:
        ValueError: The value is not a valid GitHub `owner/name`.
    """
    if not isinstance(full_name, str) or not _FULL_NAME_RE.fullmatch(full_name):
        raise ValueError(f"Invalid repository name: {full_name!r}")
    name = full_name.split("/", 1)[1]
    if set(name) == {"."}:
        raise ValueError(f"Invalid repository name: {full_name!r}")
    return full_name


def _validate_number(number: int) -> int:
    # bool is a subclass of int; refuse it so True does not read item 1.
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError(f"Invalid issue or pull request number: {number!r}")
    return number


def meta_dir(full_name: str) -> Path:
    """Return the metadata folder of a repository (it may not exist yet).

    Args:
        full_name: The repository as `owner/name`.

    Raises:
        ValueError: `full_name` is invalid or points outside the mirror root.
    """
    owner, name = validate_full_name(full_name).split("/", 1)
    root = runtime.mirror_root().resolve()
    path = (root / owner / f"{name}.meta").resolve()
    # Second line of defence (for example a symlinked owner folder).
    if not path.is_relative_to(root):
        raise ValueError(f"Invalid repository name: {full_name!r}")
    return path


def item_path(full_name: str, kind: str, number: int) -> Path:
    """Return the file path of one issue or pull request."""
    if kind not in _KIND_DIRS:
        raise ValueError(f"Invalid kind: {kind!r}")
    return meta_dir(full_name) / _KIND_DIRS[kind] / f"{_validate_number(number)}.json"


# --- low-level read and write -----------------------------------------------

def atomic_write_text(path: Path, text: str) -> None:
    """Write a file so that a reader sees the old or the new content, never a part.

    The data goes to a temporary file in the same folder, then `os.replace`
    moves it over the target. A crash leaves at most a `*.tmp` file, which
    `remove_temp_files` deletes on the next run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}{_TMP_SUFFIX}")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _replace(tmp: Path, path: Path) -> None:
    # On Windows and on SMB shares the replace fails while a reader has the
    # target open. That state is short, so try again a few times.
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1 * (attempt + 1))


def atomic_write_json(path: Path, data: Any) -> None:
    """Write `data` as UTF-8 JSON (indent 1, non-ASCII kept) with an atomic replace."""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def read_json(path: Path) -> Any | None:
    """Return the parsed JSON file, or None when it is missing or not valid JSON."""
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
        return None


def remove_temp_files(full_name: str) -> int:
    """Delete temporary files that a crashed run left behind. Returns the count."""
    base = meta_dir(full_name)
    removed = 0
    for folder in (base, base / "issues", base / "pulls"):
        if not folder.is_dir():
            continue
        for tmp in folder.glob(f".*{_TMP_SUFFIX}"):
            tmp.unlink(missing_ok=True)
            removed += 1
    return removed


def write_readme(full_name: str) -> None:
    """Write README.txt when it is missing or from another format version."""
    path = meta_dir(full_name) / "README.txt"
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = None
    if current != _README:
        atomic_write_text(path, _README)


# --- index ------------------------------------------------------------------

def index_entry(kind: str, doc: dict) -> dict:
    """Build the compact index entry for one stored issue or pull request file.

    Keys: `number`, `kind` ("issue" or "pull"), `title`, `state` ("open" or
    "closed"), `state_reason`, `merged` (pull requests: bool, issues: None),
    `draft`, `user` (login or None), `labels` (list of `{"name", "color"}`),
    `created_at`, `updated_at`, `closed_at`, `merged_at`, `comments` (count of
    conversation comments), `review_comments` and `reviews` (counts, 0 for
    issues), `fetched_at` (GitHub server time of the download, or None).
    """
    obj = doc.get(kind) or {}
    is_pull = kind == KIND_PULL
    labels = [
        {"name": lb.get("name"), "color": lb.get("color")}
        for lb in obj.get("labels") or [] if isinstance(lb, dict)
    ]
    return {
        "number": obj.get("number"),
        "kind": kind,
        "title": obj.get("title") or "",
        "state": obj.get("state"),
        "state_reason": obj.get("state_reason"),
        "merged": bool(obj.get("merged") or obj.get("merged_at")) if is_pull else None,
        "draft": bool(obj.get("draft")),
        "user": (obj.get("user") or {}).get("login"),
        "labels": labels,
        "created_at": obj.get("created_at"),
        "updated_at": obj.get("updated_at"),
        "closed_at": obj.get("closed_at"),
        "merged_at": obj.get("merged_at") if is_pull else None,
        "comments": len(doc.get("comments") or []),
        "review_comments": len(doc.get("review_comments") or []),
        "reviews": len(doc.get("reviews") or []),
        "fetched_at": doc.get("fetched_at"),
    }


def write_index(full_name: str, entries: list[dict]) -> None:
    """Write index.json, sorted by number then kind."""
    items = sorted(entries, key=lambda e: (e.get("number") or 0, e.get("kind") or ""))
    atomic_write_json(meta_dir(full_name) / "index.json",
                      {"format_version": FORMAT_VERSION, "items": items})


def rebuild_index(full_name: str) -> list[dict]:
    """Read every issue and pull request file and build the index entries.

    This is the repair path after a crash. It does not write index.json.
    """
    base = meta_dir(full_name)
    entries: list[dict] = []
    for kind, folder in _KIND_DIRS.items():
        if not (base / folder).is_dir():
            continue
        for path in (base / folder).glob("*.json"):
            if path.name.startswith("."):
                continue
            doc = read_json(path)
            if isinstance(doc, dict) and isinstance(doc.get(kind), dict):
                entries.append(index_entry(kind, doc))
    return entries


def _tree_bytes(folder: Path, skip: frozenset[str] = frozenset()) -> int:
    total = 0
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.name in skip:
                continue
            if entry.is_dir(follow_symlinks=False):
                total += _tree_bytes(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
    return total


def meta_bytes(full_name: str) -> int | None:
    """Return the size of the metadata folder without `assets/`, or None.

    The release files in `assets/` have their own size (`repos.assets_bytes`).
    Jobs call this; pages never do, because it reads every file entry on the NAS.

    Returns:
        The size in bytes, 0 when the folder does not exist yet, or None when
        the folder cannot be read.
    """
    folder = meta_dir(full_name)
    if not folder.is_dir():
        return 0
    try:
        return _tree_bytes(folder, frozenset({"assets"}))
    except OSError:
        return None


# --- read API ---------------------------------------------------------------

def has_metadata(full_name: str) -> bool:
    """Return True when the repository has a captured index (at least one run got that far)."""
    return (meta_dir(full_name) / "index.json").is_file()


def load_index(full_name: str) -> list[dict]:
    """Return all index entries (see `index_entry` for the keys), sorted by number.

    Returns an empty list when the repository has no metadata.
    """
    data = read_json(meta_dir(full_name) / "index.json")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return []
    return [e for e in data["items"] if isinstance(e, dict)]


def _matches(entry: dict, kind: str | None, state: str | None, needle: str) -> bool:
    if kind and entry.get("kind") != kind:
        return False
    if state == "merged":
        if not entry.get("merged"):
            return False
    elif state and entry.get("state") != state:
        return False
    if needle:
        if needle.lstrip("#").isdigit():
            if str(entry.get("number")) == needle.lstrip("#"):
                return True
        haystack = " ".join([
            entry.get("title") or "", entry.get("user") or "",
            *[(lb.get("name") or "") for lb in entry.get("labels") or []],
        ]).lower()
        if needle not in haystack:
            return False
    return True


def list_items(full_name: str, kind: str | None = None, state: str | None = None,
               query: str | None = None, page: int = 1,
               per_page: int = 50) -> tuple[list[dict], int]:
    """Return one page of index entries plus the total number of matches.

    Args:
        full_name: The repository as `owner/name`.
        kind: "issue", "pull", or None for both.
        state: "open", "closed" (for pull requests this includes merged ones),
            "merged" (merged pull requests only), or None for all.
        query: Text to find, not case-sensitive, in the title, the author login
            and the label names. A number (`12` or `#12`) also finds that item.
        page: Page number, starts at 1. A page after the last one is empty.
        per_page: Page size, limited to the range 1 to 500.

    Returns:
        `(items, total)`. The order is newest first (highest number first).

    Raises:
        ValueError: `full_name`, `kind` or `state` is invalid.
    """
    if kind not in (None, KIND_ISSUE, KIND_PULL):
        raise ValueError(f"Invalid kind: {kind!r}")
    if state not in (None, "open", "closed", "merged"):
        raise ValueError(f"Invalid state: {state!r}")
    needle = (query or "").strip().lower()
    matches = [e for e in load_index(full_name) if _matches(e, kind, state, needle)]
    matches.sort(key=lambda e: e.get("number") or 0, reverse=True)
    page = max(1, int(page))
    per_page = min(500, max(1, int(per_page)))
    start = (page - 1) * per_page
    return matches[start:start + per_page], len(matches)


def load_issue(full_name: str, number: int) -> dict | None:
    """Return `{"issue": {...}, "comments": [...], "fetched_at": ...}` or None.

    `issue` is the GitHub issue object. `comments` are GitHub issue comment
    objects, oldest first. A pull request number gives None here: use `load_pull`.
    """
    doc = read_json(item_path(full_name, KIND_ISSUE, number))
    return doc if isinstance(doc, dict) else None


def load_pull(full_name: str, number: int) -> dict | None:
    """Return the stored pull request document, or None.

    Keys: `pull` (the full GitHub pull request object), `comments` (issue
    comments: the conversation), `review_comments` (comments on diff lines),
    `reviews` (review objects with `state` and `body`), `commits` (light
    commit objects: `sha`, `html_url`, `commit.message`, `commit.author`,
    `commit.committer`, `author.login`), `fetched_at`.
    """
    doc = read_json(item_path(full_name, KIND_PULL, number))
    return doc if isinstance(doc, dict) else None


def _load_list(full_name: str, file_name: str) -> list[dict]:
    data = read_json(meta_dir(full_name) / file_name)
    return data if isinstance(data, list) else []


def load_releases(full_name: str) -> list[dict]:
    """Return the GitHub release objects (with `assets` metadata), newest first as GitHub sent."""
    return _load_list(full_name, "releases.json")


def load_labels(full_name: str) -> list[dict]:
    """Return the GitHub label objects of the repository."""
    return _load_list(full_name, "labels.json")


def load_milestones(full_name: str) -> list[dict]:
    """Return the GitHub milestone objects (open and closed)."""
    return _load_list(full_name, "milestones.json")


def load_repo(full_name: str) -> dict | None:
    """Return the GitHub repository object, or None."""
    data = read_json(meta_dir(full_name) / "repo.json")
    return data if isinstance(data, dict) else None


def load_state(full_name: str) -> dict:
    """Return state.json (sync cursor, counts, last status), or `{}` when missing."""
    data = read_json(meta_dir(full_name) / "state.json")
    return data if isinstance(data, dict) else {}


def summary(full_name: str) -> dict:
    """Return counts by kind and state.

    Returns:
        `{"issues": {"open", "closed", "total"},
          "pulls": {"open", "closed", "merged", "total"},
          "releases": int}`.
        For pull requests `closed` includes the merged ones, so
        `open + closed == total` for each kind.
    """
    issues = {"open": 0, "closed": 0, "total": 0}
    pulls = {"open": 0, "closed": 0, "merged": 0, "total": 0}
    for entry in load_index(full_name):
        bucket = pulls if entry.get("kind") == KIND_PULL else issues
        bucket["total"] += 1
        if entry.get("state") in ("open", "closed"):
            bucket[entry["state"]] += 1
        if entry.get("merged"):
            pulls["merged"] += 1
    return {"issues": issues, "pulls": pulls, "releases": len(load_releases(full_name))}
