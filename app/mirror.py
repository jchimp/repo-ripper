"""Git mirroring.

`git clone --mirror` captures every ref (branches, tags, notes) with full
history and no depth limit. Subsequent runs use `git remote update --prune` to
stay in lockstep with GitHub, including deletions.

The token is passed per-command via `http.extraHeader` so it never lands in the
mirror's persisted config on the NAS. Text that git prints is scrubbed before
it is stored, in case a git version echoes the header. With no token, git runs
with no auth header: that is sufficient for public repositories.
"""
import base64
import os
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import runtime

# Where the clone URLs start. Tests point this at a folder of local bare
# repositories, so that a real clone runs without the network.
GIT_BASE_URL = "https://github.com"

GIT_TIMEOUT = 3600          # seconds, one clone or update
FSCK_TIMEOUT = 1800         # seconds, one `git fsck`
RETRY_DELAY = 5.0           # seconds before the one retry on a network error
_STDERR_TAIL = 1500         # characters of git output kept in a message

# A clone runs in this sibling folder and is renamed when complete, so that a
# clone that dies never looks like a mirror.
_PARTIAL_SUFFIX = ".partial"

# Lower-case fragments of git/curl messages that mean "the network failed",
# not "the repository is wrong". Only these get the one retry.
_NETWORK_MARKERS = (
    "could not resolve host", "connection timed out", "connection reset",
    "connection refused", "failed to connect", "operation timed out",
    "early eof", "rpc failed", "unexpected disconnect", "remote end hung up",
    "gnutls", "ssl_read", "ssl_connect", "tls connection",
    "returned error: 5", "http 5",
)


@dataclass
class MirrorResult:
    """The result of one mirror step."""
    ok: bool
    action: str                 # 'clone' or 'update'
    message: str
    size_bytes: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        """'ok', 'warn' (done, but with a problem to read) or 'error'."""
        if not self.ok:
            return "error"
        return "warn" if self.warnings else "ok"


def _auth_header(token: str) -> str:
    raw = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"http.extraHeader=Authorization: Basic {raw}"


def _git_base(token: str) -> list[str]:
    """Start of a git command. No auth header when the token is empty:
    an empty or wrong credential makes GitHub refuse a public repository."""
    if not token:
        return ["git"]
    return ["git", "-c", _auth_header(token)]


def _git() -> list[str]:
    """Start of every git command that talks to GitHub."""
    return _git_base(runtime.github_token())


def clone_url(full_name: str) -> str:
    """Return the URL that git clones for `owner/name`.

    The wiki of a repository is `owner/name.wiki`. Tests replace this function
    to point at a local repository.
    """
    return f"{GIT_BASE_URL}/{full_name}.git"


def mirror_dir(full_name: str) -> Path:
    """Return the folder of the mirror of `owner/name`."""
    return runtime.mirror_root() / f"{full_name}.git"


def scrub(text: str) -> str:
    """Remove the token and the auth header from text that will be stored."""
    token = runtime.github_token()
    if token:
        text = text.replace(_auth_header(token).split(" ")[-1], "***")
        text = text.replace(token, "***")
    text = re.sub(r"(Authorization:\s*\w+\s+)\S+", r"\1***", text, flags=re.IGNORECASE)
    # Credentials inside a URL: https://user:secret@host/
    return re.sub(r"(://)[^/\s:@]+:[^/\s@]+@", r"\1***@", text)


def dir_size(path: Path) -> int | None:
    """Return the total size in bytes of the files under `path`.

    Symbolic links are not followed. Returns None if the folder cannot be read.
    """
    total = 0
    stack = [str(path)]
    try:
        while stack:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
    except OSError:
        return None
    return total


def _run(cmd: list[str], timeout: int = GIT_TIMEOUT) -> tuple[int, str]:
    """Run a command. Returns (exit code, scrubbed stderr-or-stdout tail)."""
    try:
        # No prompt: a deleted or private repository must fail at once, not
        # wait for a user name until the timeout.
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             encoding="utf-8", errors="replace",
                             stdin=subprocess.DEVNULL,
                             env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout} s"
    except OSError as exc:
        return 127, f"cannot start {cmd[0]}: {exc}"
    text = (res.stderr or "").strip() or (res.stdout or "").strip()
    return res.returncode, scrub(text)[-_STDERR_TAIL:]


def _is_network_error(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _NETWORK_MARKERS)


def _run_with_retry(cmd: list[str], before_retry=None) -> tuple[int, str, bool]:
    """Run a git command; retry one time when the failure is a network error.

    Returns (exit code, output tail, retried).
    """
    code, text = _run(cmd)
    if code == 0 or not _is_network_error(text):
        return code, text, False
    time.sleep(RETRY_DELAY)
    if before_retry:
        before_retry()
    code, text = _run(cmd)
    return code, text, True


def _rmtree(path: Path) -> None:
    """Remove a folder tree. Git marks object files read-only; clear that first."""
    def _on_error(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)
    shutil.rmtree(path, onexc=_on_error)


def _rename(src: Path, dst: Path) -> None:
    """Rename a folder. On Windows a scanner can hold a new file open for a moment."""
    for attempt in range(5):
        try:
            src.rename(dst)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.3)


def _clone(url: str, dest: Path) -> tuple[int, str, bool]:
    """Clone into a `.partial` sibling, then rename it to `dest`."""
    partial = dest.with_name(dest.name + _PARTIAL_SUFFIX)

    def _clean() -> None:
        if partial.exists():
            _rmtree(partial)

    try:
        # Left by a run that died. Only this function makes `.partial` folders.
        _clean()
        if dest.exists():
            # No HEAD file: not a repository, a clone that an older version
            # left half done. It blocks the new clone, so remove it.
            _rmtree(dest)
    except OSError as exc:
        return 1, f"cannot remove the incomplete folder {dest.name}: {exc}", False

    code, text, retried = _run_with_retry(
        _git() + ["clone", "--mirror", url, str(partial)], before_retry=_clean)
    if code != 0:
        try:
            _clean()
        except OSError:
            pass  # the next run removes it before it clones
        return code, text, retried
    try:
        _rename(partial, dest)
    except OSError as exc:
        return 1, f"clone is complete, but the rename to {dest.name} failed: {exc}", retried
    return 0, text, retried


def _sync(url: str, dest: Path) -> tuple[bool, str, str, bool]:
    """Clone or update one bare mirror. Returns (ok, action, error text, retried)."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, "clone", f"cannot create {dest.parent}: {exc}", False
    if (dest / "HEAD").is_file():
        # Keep the remote URL current: after a rename upstream the old URL
        # works only as long as GitHub keeps the redirect.
        _run(["git", "-C", str(dest), "remote", "set-url", "origin", url], timeout=60)
        code, text, retried = _run_with_retry(
            _git() + ["-C", str(dest), "remote", "update", "--prune"])
        return code == 0, "update", text, retried
    code, text, retried = _clone(url, dest)
    return code == 0, "clone", text, retried


def mirror_repo(full_name: str, *, fetch_lfs: bool, include_wiki: bool,
                has_wiki: bool) -> MirrorResult:
    """Clone or update one mirror.

    Args:
        full_name: `owner/name`.
        fetch_lfs: Also fetch Git LFS objects.
        include_wiki: Also mirror the wiki.
        has_wiki: GitHub says that the repository can have a wiki.

    Returns:
        The result. `warnings` holds problems that did not stop the mirror
        (for example a failed LFS fetch).
    """
    dest = mirror_dir(full_name)
    ok, action, text, retried = _sync(clone_url(full_name), dest)
    if not ok:
        tail = " (after 1 retry)" if retried else ""
        return MirrorResult(False, action, f"{action} failed{tail}: {text or 'no output'}")

    notes: list[str] = [action]
    warnings: list[str] = []
    if retried:
        notes.append("ok after 1 retry")

    if fetch_lfs:
        code, lfs_text = _run(_git() + ["-C", str(dest), "lfs", "fetch", "--all"])
        if code != 0:
            notes.append("lfs failed")
            warnings.append(f"git lfs fetch failed (exit {code}): {lfs_text or 'no output'}")

    if include_wiki and has_wiki:
        # Most repositories have the wiki switch on but no wiki content, and
        # then the clone fails. That is not a problem to report.
        wiki_ok = _sync(clone_url(f"{full_name}.wiki"), mirror_dir(f"{full_name}.wiki"))[0]
        notes.append("wiki ok" if wiki_ok else "wiki absent")

    return MirrorResult(True, action, ", ".join(notes), dir_size(dest), warnings)


def verify_mirror(full_name: str) -> tuple[bool, str]:
    """Run `git fsck` on one mirror. Returns (ok, message)."""
    dest = mirror_dir(full_name)
    code, text = _run(["git", "-C", str(dest), "fsck", "--no-dangling"], timeout=FSCK_TIMEOUT)
    if code == 0:
        return True, "fsck ok"
    return False, f"fsck failed (exit {code}): {text or 'no output'}"
