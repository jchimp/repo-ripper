"""Git mirroring.

`git clone --mirror` captures every ref (branches, tags, notes) with full
history and no depth limit. Subsequent runs use `git remote update --prune` to
stay in lockstep with GitHub, including deletions.

The token is passed per-command via `http.extraHeader` so it never lands in the
mirror's persisted config on the NAS. With no token, git runs with no auth
header: that is sufficient for public repositories.
"""
import base64
import subprocess
from pathlib import Path

from . import runtime

# Where the clone URLs start. Tests point this at a folder of local bare
# repositories, so that a real clone runs without the network.
GIT_BASE_URL = "https://github.com"


def _auth_header(token: str) -> str:
    raw = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"http.extraHeader=Authorization: Basic {raw}"


def _git_base(token: str) -> list[str]:
    """Start of every git command. No auth header when the token is empty:
    an empty or wrong credential makes GitHub refuse a public repository."""
    if not token:
        return ["git"]
    return ["git", "-c", _auth_header(token)]


def _clone_url(full_name: str, wiki: bool = False) -> str:
    return f"{GIT_BASE_URL}/{full_name}{'.wiki' if wiki else ''}.git"


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
    git = _git_base(runtime.github_token())
    dest = runtime.mirror_root() / f"{full_name}.git"
    clone_url = _clone_url(full_name)

    dest.parent.mkdir(parents=True, exist_ok=True)

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
        wiki_ok = _mirror_wiki(full_name, git)
        notes.append("wiki ok" if wiki_ok else "wiki absent")

    return True, ", ".join(notes), _dir_size(dest)


def _mirror_wiki(full_name: str, git: list[str]) -> bool:
    dest = runtime.mirror_root() / f"{full_name}.wiki.git"
    url = _clone_url(full_name, wiki=True)
    if (dest / "HEAD").exists():
        res = _run(git + ["-C", str(dest), "remote", "update", "--prune"])
    else:
        res = _run(git + ["clone", "--mirror", url, str(dest)])
    return res.returncode == 0
