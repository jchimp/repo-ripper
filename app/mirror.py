"""Git mirroring.

`git clone --mirror` captures every ref (branches, tags, notes) with full
history and no depth limit. Subsequent runs use `git remote update --prune` to
stay in lockstep with GitHub, including deletions.

The token is passed per-command via `http.extraHeader` so it never lands in the
mirror's persisted config on the NAS.
"""
import base64
import subprocess
from pathlib import Path

from .config import get_settings


def _auth_header(token: str) -> str:
    raw = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"http.extraHeader=Authorization: Basic {raw}"


def _run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _dir_size(path: Path) -> int | None:
    res = _run(["du", "-sb", str(path)], timeout=120)
    if res.returncode != 0:
        return None
    try:
        return int(res.stdout.split()[0])
    except (ValueError, IndexError):
        return None


def mirror_repo(full_name: str, *, fetch_lfs: bool, include_wiki: bool,
                has_wiki: bool) -> tuple[bool, str, int | None]:
    """Clone or update one mirror. Returns (ok, message, size_bytes)."""
    s = get_settings()
    header = _auth_header(s.github_token)
    dest = Path(s.mirror_root) / f"{full_name}.git"
    clone_url = f"https://github.com/{full_name}.git"

    dest.parent.mkdir(parents=True, exist_ok=True)
    git = ["git", "-c", header]

    if (dest / "HEAD").exists():
        res = _run(git + ["-C", str(dest), "remote", "update", "--prune"])
        action = "update"
    else:
        res = _run(git + ["clone", "--mirror", clone_url, str(dest)])
        action = "clone"

    if res.returncode != 0:
        return False, f"{action} failed: {res.stderr.strip()[-1500:]}", None

    notes: list[str] = [action]

    if fetch_lfs:
        lfs = _run(git + ["-C", str(dest), "lfs", "fetch", "--all"])
        if lfs.returncode != 0:
            notes.append("lfs skipped")

    if include_wiki and has_wiki:
        wiki_ok = _mirror_wiki(full_name, header)
        notes.append("wiki ok" if wiki_ok else "wiki absent")

    return True, ", ".join(notes), _dir_size(dest)


def _mirror_wiki(full_name: str, header: str) -> bool:
    s = get_settings()
    dest = Path(s.mirror_root) / f"{full_name}.wiki.git"
    url = f"https://github.com/{full_name}.wiki.git"
    git = ["git", "-c", header]
    if (dest / "HEAD").exists():
        res = _run(git + ["-C", str(dest), "remote", "update", "--prune"])
    else:
        res = _run(git + ["clone", "--mirror", url, str(dest)])
    return res.returncode == 0
