"""Git mirroring.

`git clone --mirror` captures every ref (branches, tags, notes) with full
history and no depth limit. Subsequent runs use `git remote update --prune`,
so the list of branches and tags in the mirror is the list on GitHub.

The commits are kept. A branch that is deleted upstream, or a force-push,
removes the name of the old commits, not the commits: each mirror is set so
that git never removes unreachable objects, and each update writes the lost
names and their object ids to `repo-ripper-lost-refs.log` in the mirror.

The token goes to git through the environment (`GIT_CONFIG_*`), as an
`http.<GitHub URL>.extraHeader` value. So it is not in the mirror's config on
the NAS, not in the process list, and git sends it only to the GitHub host.
Text that git prints is scrubbed before it is stored, in case a git version
echoes the header. With no token, git runs with no auth header: that is
sufficient for public repositories.
"""
import base64
import os
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import runtime

# Where the clone URLs start. Tests point this at a folder of local bare
# repositories, so that a real clone runs without the network.
GIT_BASE_URL = "https://github.com"

GIT_TIMEOUT = 3600          # seconds, one clone or update
FSCK_TIMEOUT = 1800         # seconds, one `git fsck`
LOCAL_TIMEOUT = 300         # seconds, one git command that reads or sets up a mirror
RETRY_DELAY = 5.0           # seconds before the one retry on a network error
_STDERR_TAIL = 1500         # characters of git output kept in a message

# A clone runs in this sibling folder and is renamed when complete, so that a
# clone that dies never looks like a mirror.
_PARTIAL_SUFFIX = ".partial"

# The file in each mirror that lists the branches and tags that were deleted or
# overwritten upstream, with the object id that each one had.
LOST_REFS_LOG = "repo-ripper-lost-refs.log"

# Set in each mirror, so that a commit that loses its name upstream stays on
# disk. `gc.pruneExpire=never` is the one that keeps the objects. The reflog
# values are an aid only: `git reflog` shows the old tips of a force-pushed
# branch, but not the tip that the clone made (a clone writes no reflog), and
# git removes the reflog of a deleted branch. The log file covers all cases.
_KEEP_CONFIG = (
    ("gc.pruneExpire", "never"),
    ("core.logAllRefUpdates", "always"),
    ("gc.reflogExpire", "never"),
    ("gc.reflogExpireUnreachable", "never"),
)

# GitHub makes `refs/pull/<n>/merge` again each time the target branch moves.
# These are test merges, not work of a person: do not fill the log with them.
_GENERATED_REF = re.compile(r"refs/pull/\d+/merge")

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


@dataclass
class _SyncResult:
    """The result of the clone or update of one bare mirror."""
    ok: bool
    action: str                 # 'clone' or 'update'
    text: str                   # git output; the error text when not ok
    retried: bool = False
    kept: int = 0               # lines added to the lost refs log
    warnings: list[str] = field(default_factory=list)


def _basic_credential(token: str) -> str:
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


def _auth_header(token: str) -> str:
    return f"Authorization: Basic {_basic_credential(token)}"


def _auth_env(token: str) -> dict[str, str]:
    """Environment values that give git the token, for the GitHub host only.

    The environment keeps the token out of the argument list, which other
    processes on the host can read. The key holds the base URL, so git (and
    git-lfs) does not send the header to a different host, for example to the
    storage host of LFS objects. No header when the token is empty (an empty
    or wrong credential makes GitHub refuse a public repository) or when the
    base is not an http(s) URL.
    """
    base = GIT_BASE_URL.rstrip("/")
    if not token or not base.lower().startswith(("https://", "http://")):
        return {}
    try:
        # Do not replace config values that the container already passes this way.
        index = max(int(os.environ.get("GIT_CONFIG_COUNT", "0")), 0)
    except ValueError:
        index = 0
    return {
        "GIT_CONFIG_COUNT": str(index + 1),
        f"GIT_CONFIG_KEY_{index}": f"http.{base}/.extraHeader",
        f"GIT_CONFIG_VALUE_{index}": _auth_header(token),
    }


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
        text = text.replace(_basic_credential(token), "***")
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


def _exec(cmd: list[str], timeout: int, auth: bool = False) -> tuple[int, str, str]:
    """Run a command. Returns (exit code, stdout, stderr), not scrubbed.

    `auth` adds the token to the environment: only for a command that talks to
    GitHub.
    """
    # No prompt: a deleted or private repository must fail at once, not wait
    # for a user name until the timeout.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if auth:
        env.update(_auth_env(runtime.github_token()))
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             encoding="utf-8", errors="replace",
                             stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout} s"
    except OSError as exc:
        return 127, "", f"cannot start {cmd[0]}: {exc}"
    return res.returncode, res.stdout or "", res.stderr or ""


def _run(cmd: list[str], timeout: int = GIT_TIMEOUT, auth: bool = False) -> tuple[int, str]:
    """Run a command. Returns (exit code, scrubbed stderr-or-stdout tail)."""
    code, out, err = _exec(cmd, timeout, auth)
    text = err.strip() or out.strip()
    return code, scrub(text)[-_STDERR_TAIL:]


def _is_network_error(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _NETWORK_MARKERS)


def _run_with_retry(cmd: list[str], before_retry=None) -> tuple[int, str, bool]:
    """Run a git command that talks to GitHub; retry one time on a network error.

    Returns (exit code, output tail, retried).
    """
    code, text = _run(cmd, auth=True)
    if code == 0 or not _is_network_error(text):
        return code, text, False
    time.sleep(RETRY_DELAY)
    if before_retry:
        before_retry()
    code, text = _run(cmd, auth=True)
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


def _keep_unreachable_objects(dest: Path) -> str | None:
    """Set the `_KEEP_CONFIG` values in one mirror. Returns the error text, or None.

    One read, and a write only for a value that is not there: a mirror that is
    set up gets no config write on the NAS.
    """
    code, out, err = _exec(["git", "-C", str(dest), "config", "--local", "--list"],
                           LOCAL_TIMEOUT)
    if code != 0:
        return scrub(err.strip() or out.strip())[-_STDERR_TAIL:] or f"git config: exit {code}"
    have: dict[str, str] = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        have[key.lower()] = value
    for key, value in _KEEP_CONFIG:
        if have.get(key.lower()) == value:
            continue
        code, text = _run(["git", "-C", str(dest), "config", "--local", key, value],
                          timeout=LOCAL_TIMEOUT)
        if code != 0:
            return text or f"git config {key}: exit {code}"
    return None


def _read_refs(dest: Path) -> dict[str, tuple[str, str]] | None:
    """Return {ref name: (object id, object type)} of one mirror, or None on an error."""
    code, out, _err = _exec(
        ["git", "-C", str(dest), "for-each-ref",
         "--format=%(objectname) %(objecttype) %(refname)"], LOCAL_TIMEOUT)
    if code != 0:
        return None
    refs: dict[str, tuple[str, str]] = {}
    for line in out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3:
            refs[parts[2]] = (parts[0], parts[1])
    return refs


def _is_ancestor(dest: Path, old: str, new: str) -> bool:
    """True if commit `old` is in the history of commit `new`. An error is False."""
    code, _text = _run(["git", "-C", str(dest), "merge-base", "--is-ancestor",
                        "--end-of-options", old, new], timeout=LOCAL_TIMEOUT)
    return code == 0


def _lost_refs(dest: Path, before: dict[str, tuple[str, str]],
               after: dict[str, tuple[str, str]]) -> list[str]:
    """Return one log entry (with no time) for each ref that lost commits in an update."""
    entries: list[str] = []
    for ref, (old, old_type) in before.items():
        if _GENERATED_REF.fullmatch(ref):
            continue
        if ref not in after:
            entries.append(f"deleted {ref} {old}")
            continue
        new, new_type = after[ref]
        if new == old:
            continue
        # A tag object that is replaced loses its name in all cases. For two
        # commits, a fast-forward keeps the old commit in the history.
        if old_type != "commit" or new_type != "commit" or not _is_ancestor(dest, old, new):
            entries.append(f"forced {ref} {old} {new}")
    return entries


def _record_lost_refs(dest: Path, before: dict[str, tuple[str, str]] | None
                      ) -> tuple[int, str | None]:
    """Write the refs that an update removed or overwrote to the log in the mirror.

    Returns (lines written, warning text or None). The objects stay in the
    mirror in all cases; the log only tells where they are.
    """
    after = _read_refs(dest)
    if before is None or after is None:
        return 0, ("cannot read the list of branches and tags: deleted or overwritten "
                   f"branches of this run are not in {LOST_REFS_LOG}")
    entries = _lost_refs(dest, before, after)
    if not entries:
        return 0, None
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        # Append and flush to disk: a temporary file and a rename (the rule for
        # NAS files) would copy the full log on each update.
        with (dest / LOST_REFS_LOG).open("a", encoding="utf-8", newline="\n") as handle:
            handle.writelines(f"{stamp} {entry}\n" for entry in entries)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        return 0, (f"cannot write {LOST_REFS_LOG} ({exc}). The old commits are in the "
                   f"mirror, but their ids are not recorded: {'; '.join(entries)}")
    return len(entries), None


def _clone(url: str, dest: Path) -> tuple[int, str, bool, str | None]:
    """Clone into a `.partial` sibling, then rename it to `dest`.

    Returns (exit code, output tail, retried, note). The note says that a
    folder that was in the way was moved aside.
    """
    partial = dest.with_name(dest.name + _PARTIAL_SUFFIX)
    moved: str | None = None

    def _clean() -> None:
        if partial.exists():
            _rmtree(partial)

    try:
        # Left by a run that died. Only this function makes `.partial` folders.
        _clean()
    except OSError as exc:
        return 1, f"cannot remove the incomplete folder {partial.name}: {exc}", False, None
    if dest.exists():
        # No HEAD file: a clone that an older version left half done, or a
        # damaged mirror. The two look the same, and a damaged mirror can hold
        # the only copy. It blocks the new clone, so move it aside; never
        # delete it.
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        broken = dest.with_name(f"{dest.name}.broken-{stamp}")
        try:
            _rename(dest, broken)
        except OSError as exc:
            return 1, f"cannot move the incomplete folder {dest.name} aside: {exc}", False, None
        moved = (f"the folder {dest.name} was not a git repository (no HEAD file). "
                 f"It was renamed to {broken.name} and is kept. Delete it by hand if "
                 "you do not need it")

    code, text, retried = _run_with_retry(
        ["git", "clone", "--mirror", url, str(partial)], before_retry=_clean)
    if code != 0:
        try:
            _clean()
        except OSError:
            pass  # the next run removes it before it clones
        return code, text, retried, moved
    try:
        _rename(partial, dest)
    except OSError as exc:
        return (1, f"clone is complete, but the rename to {dest.name} failed: {exc}",
                retried, moved)
    return 0, text, retried, moved


def _sync(url: str, dest: Path) -> _SyncResult:
    """Clone or update one bare mirror."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _SyncResult(False, "clone", f"cannot create {dest.parent}: {exc}")
    warnings: list[str] = []
    if (dest / "HEAD").is_file():
        # Keep the remote URL current: after a rename upstream the old URL
        # works only as long as GitHub keeps the redirect.
        _run(["git", "-C", str(dest), "remote", "set-url", "origin", url], timeout=60)
        # Before the update: the update can start a garbage collection.
        problem = _keep_unreachable_objects(dest)
        if problem:
            warnings.append(f"cannot set the git options that keep old commits: {problem}")
        before = _read_refs(dest)
        # `--prune` stays: without it a deleted branch `a` blocks a new branch `a/b`.
        code, text, retried = _run_with_retry(
            ["git", "-C", str(dest), "remote", "update", "--prune"])
        # Also after a failed update: it can change a part of the refs.
        kept, problem = _record_lost_refs(dest, before)
        if problem:
            warnings.append(problem)
        return _SyncResult(code == 0, "update", text, retried, kept, warnings)
    code, text, retried, moved = _clone(url, dest)
    if moved:
        warnings.append(moved)
    if code == 0:
        problem = _keep_unreachable_objects(dest)
        if problem:
            warnings.append(f"cannot set the git options that keep old commits: {problem}")
    return _SyncResult(code == 0, "clone", text, retried, 0, warnings)


def _kept_note(count: int) -> str:
    return f"{count} old ref{'' if count == 1 else 's'} kept"


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
    res = _sync(clone_url(full_name), dest)
    if not res.ok:
        tail = " (after 1 retry)" if res.retried else ""
        message = f"{res.action} failed{tail}: {res.text or 'no output'}"
        if res.warnings:
            # For example: the folder that was moved aside before the clone.
            message += "\n" + "\n".join(res.warnings)
        return MirrorResult(False, res.action, message)

    notes: list[str] = [res.action]
    warnings: list[str] = list(res.warnings)
    if res.retried:
        notes.append("ok after 1 retry")
    if res.kept:
        notes.append(_kept_note(res.kept))

    if fetch_lfs:
        code, lfs_text = _run(["git", "-C", str(dest), "lfs", "fetch", "--all"], auth=True)
        if code != 0:
            notes.append("lfs failed")
            warnings.append(f"git lfs fetch failed (exit {code}): {lfs_text or 'no output'}")

    if include_wiki and has_wiki:
        wiki_dest = mirror_dir(f"{full_name}.wiki")
        wiki_on_disk = (wiki_dest / "HEAD").is_file()
        wiki = _sync(clone_url(f"{full_name}.wiki"), wiki_dest)
        warnings.extend(f"wiki: {text}" for text in wiki.warnings)
        if wiki.ok:
            notes.append("wiki ok")
            if wiki.kept:
                notes.append(f"wiki: {_kept_note(wiki.kept)}")
        elif wiki_on_disk:
            # The wiki was mirrored before, so "no wiki" is not the reason.
            notes.append("wiki failed")
            warnings.append(f"wiki update failed: {wiki.text or 'no output'}")
        else:
            # Most repositories have the wiki switch on but no wiki content,
            # and then the clone fails. That is not a problem to report.
            notes.append("wiki absent")

    return MirrorResult(True, res.action, ", ".join(notes), dir_size(dest), warnings)


def verify_mirror(full_name: str) -> tuple[bool, str]:
    """Run `git fsck` on one mirror. Returns (ok, message)."""
    dest = mirror_dir(full_name)
    code, text = _run(["git", "-C", str(dest), "fsck", "--no-dangling"], timeout=FSCK_TIMEOUT)
    if code == 0:
        return True, "fsck ok"
    return False, f"fsck failed (exit {code}): {text or 'no output'}"
