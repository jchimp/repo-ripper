"""Download the files of GitHub releases: attached files and source archives.

Layout, below the metadata folder of a repository:

    <owner>/<name>.meta/assets/
        index.json                    what is stored, and the state of each file
        <release id>/files/<name>     a file that the author attached to the release
        <release id>/source/<name>    GitHub's "Source code" archive (.zip and .tar.gz)

The list of releases comes from `releases.json`, which the metadata capture
writes. This module reads that file; it does not ask GitHub for the list.

Rules:
- Only the newest N releases get downloads (setting `assets_keep_releases`).
- A file larger than the size limit is not stored (state `too_large`).
- Nothing is deleted. A file whose release or asset is gone on GitHub keeps
  its local copy and gets `removed_upstream`.
- A file is written under a temporary name and renamed when it is complete.
"""
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import store
from .client import GitHubClient, GitHubError, NotFound, RateLimited, TooLarge

INDEX_VERSION = 1
STATE_STORED = "stored"
STATE_TOO_LARGE = "too_large"
STATE_FAILED = "failed"

# Free space that must remain on the disk after a download.
DISK_MARGIN = 500 * 1024 * 1024
_SOURCE_KINDS = {"zip": ("zipball_url", ".zip"), "tar": ("tarball_url", ".tar.gz")}
_UNSAFE = re.compile(r"[^A-Za-z0-9._+()@ -]")


class DiskFull(Exception):
    """The disk has too little free space for the next file."""


@dataclass
class AssetsResult:
    """Outcome of one repository run."""
    status: str                 # ok | partial | error | rate_limited
    message: str
    downloaded: int = 0         # files stored in this run
    downloaded_bytes: int = 0
    files: int = 0              # files on disk after the run
    total_bytes: int = 0
    reset_at: float | None = None


def assets_dir(full_name: str) -> Path:
    """Return the folder that holds the release files of a repository."""
    return store.meta_dir(full_name) / "assets"


def safe_name(name: object, fallback: str) -> str:
    """Turn a name from GitHub into one safe file name.

    The name becomes the last part of a path on the NAS, so path separators,
    control characters and the names `.` and `..` must not survive.
    """
    text = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    text = _UNSAFE.sub("_", text).strip(" .")
    return text[:200] or fallback


def load_index(full_name: str) -> dict:
    """Return the index of stored release files (`{"releases": {id: {...}}}`)."""
    data = store.read_json(assets_dir(full_name) / "index.json")
    if not isinstance(data, dict) or not isinstance(data.get("releases"), dict):
        return {"version": INDEX_VERSION, "releases": {}}
    return data


def _save_index(full_name: str, index: dict) -> None:
    store.atomic_write_json(assets_dir(full_name) / "index.json", index)


def totals(index: dict) -> tuple[int, int]:
    """Return (number of stored files, their total size in bytes)."""
    count = size = 0
    for rel in index["releases"].values():
        for entry in [*rel.get("files", {}).values(), *rel.get("source", {}).values()]:
            if entry.get("state") == STATE_STORED:
                count += 1
                size += int(entry.get("size") or 0)
    return count, size


def stored_path(full_name: str, release_id: str, group: str, key: str) -> Path | None:
    """Return the path of one stored file, or None when it is not stored.

    Args:
        full_name: The repository as `owner/name`.
        release_id: Key of the release in the index.
        group: "files" (attached files, key = asset id) or "source" (key = zip|tar).
        key: The key inside the group.
    """
    if group not in ("files", "source"):
        return None
    entry = load_index(full_name)["releases"].get(release_id, {}).get(group, {}).get(key)
    if not isinstance(entry, dict) or entry.get("state") != STATE_STORED:
        return None
    base = assets_dir(full_name).resolve()
    path = (base / str(entry.get("path") or "")).resolve()
    # The path comes from the index file on the NAS: check it again.
    if not path.is_relative_to(base) or not path.is_file():
        return None
    return path


def _release_time(rel: dict) -> str:
    return str(rel.get("published_at") or rel.get("created_at") or "")


def _check_space(folder: Path, need: int) -> None:
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return  # cannot measure: the write itself reports a full disk
    if free < need + DISK_MARGIN:
        raise DiskFull(
            f"The disk has {free // (1024 * 1024)} MB free. The next file needs "
            f"{need // (1024 * 1024)} MB plus a reserve of {DISK_MARGIN // (1024 * 1024)} MB.")


def _fetch(client: GitHubClient, url: str, dest: Path, *, accept: str | None,
           max_bytes: int, expect_size: int | None, expect_sha256: str | None) -> dict:
    """Download one file to `dest`. Returns the index entry fields for it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _check_space(dest.parent, expect_size or 0)
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        size, sha256 = client.download(url, tmp, accept=accept, max_bytes=max_bytes)
        if expect_size is not None and size != expect_size:
            raise GitHubError(f"Size is {size} bytes, GitHub lists {expect_size} bytes.")
        if expect_sha256 and sha256 != expect_sha256:
            raise GitHubError("The SHA-256 checksum is not the one that GitHub lists.")
        store._replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return {"state": STATE_STORED, "size": size, "sha256": sha256,
            "verified": bool(expect_sha256), "stored_at": time.time(), "error": ""}


def sync_repo(client: GitHubClient, full_name: str, *, keep_releases: int,
              max_bytes: int) -> AssetsResult:
    """Download the missing release files of one repository.

    Args:
        client: An open GitHub client.
        full_name: The repository as `owner/name`.
        keep_releases: Number of newest releases that get downloads. 0 = all.
        max_bytes: Size limit for one file.

    Returns:
        The result. `rate_limited` means: stop the job, the next run continues.
    """
    releases = [r for r in store.load_releases(full_name)
                if isinstance(r, dict) and r.get("id") is not None]
    index = load_index(full_name)
    known = index["releases"]
    base = assets_dir(full_name)

    releases.sort(key=_release_time, reverse=True)
    selected = releases if keep_releases <= 0 else releases[:keep_releases]
    upstream_ids = {str(r["id"]) for r in releases}
    for rid, rel in known.items():
        rel["removed_upstream"] = rid not in upstream_ids

    done = done_bytes = failed = too_large = 0
    problems: list[str] = []
    stop: AssetsResult | None = None

    def result(status: str, message: str, reset_at: float | None = None) -> AssetsResult:
        _save_index(full_name, index)
        count, size = totals(index)
        return AssetsResult(status, message, done, done_bytes, count, size, reset_at)

    for rel in selected:
        rid = str(rel["id"])
        tag = str(rel.get("tag_name") or "")
        entry = known.setdefault(rid, {"files": {}, "source": {}})
        entry.update({"tag_name": tag, "name": str(rel.get("name") or ""),
                      "published_at": _release_time(rel), "removed_upstream": False})
        files, source = entry.setdefault("files", {}), entry.setdefault("source", {})
        rel_dir = safe_name(rid, "release")

        # --- attached files ---
        assets = [a for a in rel.get("assets") or []
                  if isinstance(a, dict) and a.get("id") is not None and a.get("url")]
        asset_ids = {str(a["id"]) for a in assets}
        for key, old in files.items():
            old["removed_upstream"] = key not in asset_ids
        used = {f.get("path") for k, f in files.items() if k not in asset_ids}
        jobs: list[tuple[dict, str, str, dict]] = []   # (index entry, url, accept, expectations)
        for asset in assets:
            key = str(asset["id"])
            name = safe_name(asset.get("name"), f"asset-{key}")
            path = f"{rel_dir}/files/{name}"
            if path in used:           # two names that became equal after cleaning
                path = f"{rel_dir}/files/{key}-{name}"
            used.add(path)
            size = asset.get("size") if isinstance(asset.get("size"), int) else None
            old = files.get(key, {})
            current = (old.get("state") == STATE_STORED and old.get("size") == size
                       and old.get("updated_at") == asset.get("updated_at")
                       and (base / str(old.get("path"))).is_file())
            digest = str(asset.get("digest") or "")
            item = files[key] = {
                **old, "name": str(asset.get("name") or name), "path": old.get("path") or path,
                "listed_size": size, "removed_upstream": False,
            }
            if current:
                continue
            item["path"] = path
            if size is not None and size > max_bytes:
                item.update({"state": STATE_TOO_LARGE, "size": size, "error": ""})
                too_large += 1
                continue
            jobs.append((item, str(asset["url"]), "application/octet-stream", {
                "expect_size": size,
                "expect_sha256": digest[7:] if digest.startswith("sha256:") else None,
                "updated_at": asset.get("updated_at"),
            }))

        # --- GitHub's source archives ---
        for kind, (field, ext) in _SOURCE_KINDS.items():
            url = rel.get(field)
            if not url:
                continue   # a draft release has no tag yet
            old = source.get(kind, {})
            name = safe_name(f"{full_name.split('/', 1)[1]}-{tag}", f"source-{rid}") + ext
            path = f"{rel_dir}/source/{name}"
            current = (old.get("state") == STATE_STORED and old.get("tag_name") == tag
                       and (base / str(old.get("path"))).is_file())
            item = source[kind] = {**old, "name": name, "tag_name": tag,
                                   "path": old.get("path") or path}
            # A source archive has no listed size. One that was too large is
            # tried again only after the size limit was raised.
            was_too_large = (old.get("state") == STATE_TOO_LARGE and old.get("tag_name") == tag
                             and old.get("limit") == max_bytes)
            if current or was_too_large:
                continue
            item["path"] = path
            jobs.append((item, str(url), "", {"expect_size": None, "expect_sha256": None,
                                              "updated_at": None}))

        for item, url, accept, expect in jobs:
            try:
                fields = _fetch(client, url, base / item["path"], accept=accept or None,
                                max_bytes=max_bytes, expect_size=expect["expect_size"],
                                expect_sha256=expect["expect_sha256"])
            except RateLimited as exc:
                stop = result("rate_limited", str(exc), exc.reset_at)
            except DiskFull as exc:
                stop = result("error", str(exc))
            except TooLarge:
                item.update({"state": STATE_TOO_LARGE, "error": "", "limit": max_bytes})
                too_large += 1
                continue
            except NotFound:
                item.update({"state": STATE_FAILED,
                             "error": "GitHub answered: not found. The file was removed."})
                failed += 1
                problems.append(f"{item['name']}: not found on GitHub")
                continue
            except (GitHubError, OSError) as exc:
                item.update({"state": STATE_FAILED, "error": str(exc)[:300]})
                failed += 1
                problems.append(f"{item['name']}: {str(exc)[:150]}")
                continue
            else:
                item.update(fields)
                if expect["updated_at"] is not None:
                    item["updated_at"] = expect["updated_at"]
                done += 1
                done_bytes += fields["size"]
                # Save after each file: a crash or a stop loses no finished download.
                _save_index(full_name, index)
                continue
            return stop

    parts = [f"{done} downloaded"]
    if too_large:
        parts.append(f"{too_large} larger than the size limit")
    if failed:
        parts.append(f"{failed} failed ({'; '.join(problems[:3])})")
    count, size = totals(index)
    message = f"{count} files on disk. This run: " + ", ".join(parts) + "."
    return result("partial" if failed else "ok", message)
