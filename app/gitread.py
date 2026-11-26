"""Read a bare git repository by calling the `git` executable. Read-only.

Every value that can come from a URL (revision, sha, path, search text) is
treated as hostile:

- No shell. Arguments go to git as a list.
- A revision is checked by `valid_rev` and then resolved to a full sha by
  `resolve`, with `--end-of-options` in front of it. All other functions take
  that sha and refuse any value that is not a full hex object name.
- A path is checked by `clean_path` and is passed after `--`, with pathspec
  magic switched off (`--literal-pathspecs`).
- git runs with a minimal environment: no system or user configuration, no
  prompt, no optional locks, no lazy fetch. No command here writes to the
  repository, and no command here runs a hook.

Results for a full sha never change, so the expensive ones are kept in small
in-process caches.
"""
import os
import re
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any

GIT_TIMEOUT = 20.0                  # seconds for one git call
MAX_OUTPUT = 8 * 1024 * 1024        # default cap on the output of one git call
MAX_TREE_ENTRIES = 5000
MAX_DIFF_BYTES = 2 * 1024 * 1024
MAX_DIFF_FILES = 300
MAX_DIFF_FILE_LINES = 3000          # a larger file diff is listed without its lines
MAX_LINE_CHARS = 2000               # longer lines (minified files) are cut
MAX_PATH_CHARS = 4096
MAX_REV_CHARS = 255

_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# Characters and sequences that git does not permit in a ref name. Refusing
# them also keeps out the revision syntax (`a..b`, `rev~3`, `rev:path`,
# `:/text`, `@{1}`), which could make git do slow or surprising work.
_BAD_REV_RE = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
_README_NAMES = ("readme.md", "readme.markdown", "readme.mdown", "readme",
                 "readme.txt", "readme.rst")


class GitError(Exception):
    """git did not run, or did not finish in time."""


# --- results ----------------------------------------------------------------

@dataclass(frozen=True)
class Ref:
    """One branch, tag or pull ref. `sha` is the commit (a tag is peeled)."""
    name: str            # short name: "main", "feature/x", "v1.0", "pull/1/head"
    full_name: str       # "refs/heads/main"
    kind: str            # "branch", "tag" or "pull"
    sha: str
    time: int            # commit date, or tagger date for an annotated tag
    subject: str


@dataclass(frozen=True)
class Refs:
    """The refs of a repository, newest first."""
    default_branch: str | None
    branches: tuple[Ref, ...]
    tags: tuple[Ref, ...]
    pulls: tuple[Ref, ...] = ()
    pull_count: int | None = None    # None: not counted


@dataclass(frozen=True)
class TreeEntry:
    """One entry of a tree. `kind` is "tree", "blob" or "commit" (a submodule)."""
    name: str
    path: str
    kind: str
    mode: str
    oid: str
    size: int | None


@dataclass(frozen=True)
class Tree:
    entries: tuple[TreeEntry, ...]
    truncated: bool


@dataclass(frozen=True)
class Blob:
    """A file. `data` holds at most the requested number of bytes."""
    path: str
    oid: str
    mode: str
    size: int
    data: bytes
    truncated: bool
    binary: bool

    def text(self) -> str:
        """Return the data as text. Bytes that are not UTF-8 become U+FFFD."""
        return self.data.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    author_name: str
    author_email: str
    author_time: int
    committer_name: str
    committer_email: str
    committer_time: int
    subject: str
    body: str = ""       # the message after the subject (only from `commit`)


@dataclass(frozen=True)
class DiffLine:
    kind: str            # "+", "-", " " or "\\" (the "no newline" note)
    old_no: int | None
    new_no: int | None
    text: str


@dataclass(frozen=True)
class Hunk:
    header: str
    lines: tuple[DiffLine, ...]


@dataclass(frozen=True)
class DiffFile:
    old_path: str
    new_path: str
    status: str          # "added", "deleted", "renamed" or "modified"
    added: int | None    # None for a binary file
    deleted: int | None
    binary: bool
    hunks: tuple[Hunk, ...]
    omitted: str = ""    # why the lines are not shown; empty when they are

    @property
    def path(self) -> str:
        return self.new_path or self.old_path

    @property
    def line_count(self) -> int:
        return sum(len(h.lines) for h in self.hunks)


@dataclass(frozen=True)
class Diff:
    """A parsed diff. `too_large` means that some or all files have no lines."""
    files: tuple[DiffFile, ...]
    total_files: int
    added: int
    deleted: int
    too_large: bool


@dataclass(frozen=True)
class GrepMatch:
    path: str
    line_no: int
    text: str


@dataclass(frozen=True)
class GrepResult:
    matches: tuple[GrepMatch, ...]
    truncated: bool


# --- validation -------------------------------------------------------------

def is_sha(value: object) -> bool:
    """Return True for a full lower-case hex object name (SHA-1 or SHA-256)."""
    return isinstance(value, str) and _SHA_RE.fullmatch(value) is not None


def valid_rev(rev: object) -> bool:
    """Return True when `rev` looks like a ref name or a hex object name.

    A leading `-` is permitted, because git permits it in a ref name. Such a
    value is safe: `resolve` puts `--end-of-options` in front of it.
    """
    if not isinstance(rev, str) or not 0 < len(rev) <= MAX_REV_CHARS:
        return False
    if _BAD_REV_RE.search(rev):
        return False
    return not (rev.startswith("/") or rev.endswith(("/", ".", ".lock")))


def clean_path(path: object) -> str | None:
    """Return the path in the form git uses, or None when it is not acceptable.

    Leading, trailing and double slashes are removed. The empty string is the
    root. `.` and `..` segments, NUL and very long values are refused.
    """
    if not isinstance(path, str) or "\x00" in path or len(path) > MAX_PATH_CHARS:
        return None
    parts = [p for p in path.split("/") if p]
    if any(p in (".", "..") for p in parts):
        return None
    return "/".join(parts)


def _need_sha(sha: object) -> str:
    if not is_sha(sha):
        raise ValueError(f"Not a full object name: {sha!r}")
    return sha  # type: ignore[return-value]


def _need_path(path: object) -> str:
    cleaned = clean_path(path)
    if cleaned is None:
        raise ValueError(f"Invalid path: {path!r}")
    return cleaned


# --- running git ------------------------------------------------------------

def _env() -> dict[str, str]:
    # Only what git needs to start. PATH finds the executable; SYSTEMROOT is
    # needed by programs on Windows.
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if k in os.environ}
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        # A partial clone must not start a network fetch for a missing object.
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
    })
    return env


def _command(repo: Path, args: list[str]) -> list[str]:
    cmd = ["git"]
    # A mirror on a NAS often belongs to another uid. Trust this one path
    # only, never `*`. Both spellings, because git compares the text.
    for spelling in dict.fromkeys((repo.as_posix(), repo.resolve().as_posix())):
        cmd += ["-c", f"safe.directory={spelling}"]
    cmd += [
        "-c", "core.quotePath=false",
        "-c", "core.fsmonitor=false",
        "--no-pager", "--no-replace-objects", "--literal-pathspecs",
        "--git-dir", str(repo),
    ]
    return cmd + args


def _run(repo: Path, args: list[str], *, max_bytes: int = MAX_OUTPUT,
         timeout: float = GIT_TIMEOUT, stdin: bytes | None = None) -> tuple[int, bytes, bool]:
    """Run git. Returns (return code, output, truncated).

    The output is read up to `max_bytes`; then git is stopped and `truncated`
    is True. stderr is discarded: it can hold paths, and no caller needs it.

    Raises:
        GitError: git is not installed, or the call took longer than `timeout`.
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            _command(repo, args), env=_env(), shell=False, creationflags=flags,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise GitError("The git program was not found on this computer.") from exc
    except OSError as exc:
        raise GitError(f"git did not start: {exc}") from exc

    if stdin is not None:
        # Small input, small output: communicate() feeds and reads together.
        try:
            out, _ = proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.communicate()
            raise GitError(f"git {args[0]} did not finish in {timeout:.0f} seconds.") from exc
        return proc.returncode, out[:max_bytes], len(out) > max_bytes

    timed_out = threading.Event()

    def _expire() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, _expire)
    timer.start()
    chunks: list[bytes] = []
    total = 0
    truncated = False
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                truncated = True
                proc.kill()
                break
        proc.stdout.close()
        code = proc.wait()
    finally:
        timer.cancel()
    if timed_out.is_set():
        raise GitError(f"git {args[0]} did not finish in {timeout:.0f} seconds.")
    return code, b"".join(chunks)[:max_bytes], truncated


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


# --- cache ------------------------------------------------------------------

_caches: list["_Cache"] = []


class _Cache:
    """A small thread-safe LRU map."""

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self.data: OrderedDict[tuple, Any] = OrderedDict()
        self.lock = threading.Lock()
        _caches.append(self)

    def get(self, key: tuple) -> Any:
        with self.lock:
            if key in self.data:
                self.data.move_to_end(key)
                return self.data[key]
        return None

    def put(self, key: tuple, value: Any) -> None:
        with self.lock:
            self.data[key] = value
            self.data.move_to_end(key)
            while len(self.data) > self.maxsize:
                self.data.popitem(last=False)


def _cached(maxsize: int) -> Callable:
    """Cache the results of a function whose arguments include a full sha.

    None is not kept: an object that is missing now can arrive with the next
    pull (for example the head commit of a pull request).
    """
    def decorate(func: Callable) -> Callable:
        cache = _Cache(maxsize)

        @wraps(func)
        def wrapper(repo: Path, *args: Any, **kwargs: Any) -> Any:
            key = (str(repo), *args, *sorted(kwargs.items()))
            hit = cache.get(key)
            if hit is not None:
                return hit
            value = func(repo, *args, **kwargs)
            if value is not None:
                cache.put(key, value)
            return value
        return wrapper
    return decorate


def clear_cache() -> None:
    """Empty all caches (for tests)."""
    for cache in _caches:
        with cache.lock:
            cache.data.clear()


# --- repository and refs ----------------------------------------------------

def repo_exists(repo: Path) -> bool:
    """Return True when `repo` is a bare repository folder (it has HEAD and objects)."""
    return (repo / "HEAD").is_file() and (repo / "objects").is_dir()


def _head_branch(repo: Path) -> str | None:
    # Reading the HEAD file costs no process. A mirror always has a symbolic HEAD.
    try:
        head = (repo / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    return head[len(prefix):] if head.startswith(prefix) else None


def _for_each_ref(repo: Path, patterns: list[str], with_subject: bool) -> list[Ref]:
    fmt = "%(refname)%00%(objectname)%00%(*objectname)%00%(creatordate:unix)%00"
    if with_subject:
        fmt += "%(subject)"
    code, out, _ = _run(repo, ["for-each-ref", "--sort=-creatordate", f"--format={fmt}",
                               "--", *patterns])
    if code != 0:
        return []
    refs: list[Ref] = []
    for line in _text(out).split("\n"):
        fields = line.split("\x00")
        if len(fields) != 5:
            continue
        full, oid, peeled, when, subject = fields
        for prefix, kind in (("refs/heads/", "branch"), ("refs/tags/", "tag"),
                             ("refs/pull/", "pull")):
            if full.startswith(prefix):
                short = full[len("refs/"):] if kind == "pull" else full[len(prefix):]
                refs.append(Ref(short, full, kind, peeled or oid,
                                int(when) if when.isdigit() else 0, subject))
                break
    return refs


def list_refs(repo: Path, *, count_pulls: bool = False, list_pulls: bool = False) -> Refs:
    """Return the branches and tags, newest first, and the default branch.

    GitHub's `refs/pull/<n>/head` refs can be many, so they are left out by
    default. `count_pulls` counts them, `list_pulls` also returns them.

    Args:
        repo: The bare repository folder.
        count_pulls: Fill `pull_count` (one more git call).
        list_pulls: Fill `pulls` and `pull_count`.
    """
    refs = _for_each_ref(repo, ["refs/heads", "refs/tags"], with_subject=True)
    branches = tuple(r for r in refs if r.kind == "branch")
    tags = tuple(r for r in refs if r.kind == "tag")
    names = {b.name for b in branches}
    default = _head_branch(repo)
    if default not in names:
        default = next((n for n in ("main", "master") if n in names), None)
        if default is None and branches:
            default = branches[0].name
    pulls: tuple[Ref, ...] = ()
    pull_count: int | None = None
    if count_pulls or list_pulls:
        found = [r for r in _for_each_ref(repo, ["refs/pull"], with_subject=False)
                 if r.name.endswith("/head")]
        pull_count = len(found)
        if list_pulls:
            pulls = tuple(found)
    return Refs(default, branches, tags, pulls, pull_count)


def resolve(repo: Path, rev: str) -> str | None:
    """Return the full commit sha of a branch, tag, ref or hex object name.

    Returns None when the value is not acceptable, does not exist, or does
    not point to a commit. When a tag and a branch have the same name, git
    selects the tag.
    """
    if not valid_rev(rev):
        return None
    code, out, _ = _run(repo, ["rev-parse", "--verify", "--quiet", "--end-of-options",
                               f"{rev}^{{commit}}"], max_bytes=4096)
    lines = _text(out).split()
    if code != 0 or not lines or not is_sha(lines[-1]):
        return None
    return lines[-1]


def commits_exist(repo: Path, shas: list[str]) -> set[str]:
    """Return the values of `shas` that are objects in the repository (one git call)."""
    wanted = [s for s in dict.fromkeys(shas) if is_sha(s)]
    if not wanted:
        return set()
    code, out, _ = _run(repo, ["cat-file", "--batch-check"],
                        stdin=("\n".join(wanted) + "\n").encode("ascii"))
    if code != 0:
        return set()
    found: set[str] = set()
    for line in _text(out).splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == "commit":
            found.add(parts[0])
    return found


@_cached(256)
def merge_base(repo: Path, a: str, b: str) -> str | None:
    """Return the best common ancestor of two commits, or None when there is none."""
    code, out, _ = _run(repo, ["merge-base", _need_sha(a), _need_sha(b)], max_bytes=4096)
    sha = _text(out).strip()
    return sha if code == 0 and is_sha(sha) else None


# --- trees and files --------------------------------------------------------

def _parse_ls_tree(out: bytes, parent: str) -> list[TreeEntry]:
    entries: list[TreeEntry] = []
    for record in _text(out).split("\x00"):
        meta, tab, name = record.partition("\t")
        fields = meta.split()
        if not tab or len(fields) != 4:
            continue
        mode, kind, oid, size = fields
        path = name if not parent else f"{parent}/{name}"
        entries.append(TreeEntry(name.rsplit("/", 1)[-1], path, kind, mode, oid,
                                 int(size) if size.isdigit() else None))
    return entries


@_cached(256)
def list_tree(repo: Path, sha: str, path: str = "") -> Tree | None:
    """Return the entries of a folder, folders first, or None when it is not a folder.

    Args:
        repo: The bare repository folder.
        sha: A full commit sha (from `resolve`).
        path: The folder, "" for the root.
    """
    path = _need_path(path)
    code, out, truncated = _run(repo, ["ls-tree", "-z", "-l", f"{_need_sha(sha)}:{path}"])
    if code != 0 and not truncated:
        return None
    entries = _parse_ls_tree(out, path)
    if truncated and entries:
        entries.pop()  # the last record can be cut
    if len(entries) > MAX_TREE_ENTRIES:
        entries, truncated = entries[:MAX_TREE_ENTRIES], True
    entries.sort(key=lambda e: (e.kind == "blob", e.name.lower(), e.name))
    return Tree(tuple(entries), truncated)


@_cached(512)
def path_info(repo: Path, sha: str, path: str) -> TreeEntry | None:
    """Return the tree entry of one path (file, folder or submodule), or None."""
    path = _need_path(path)
    if not path:
        return None
    code, out, _ = _run(repo, ["ls-tree", "-z", "-l", "--full-tree", _need_sha(sha),
                               "--", path], max_bytes=65536)
    if code != 0:
        return None
    for entry in _parse_ls_tree(out, ""):
        if entry.path == path:
            return TreeEntry(path.rsplit("/", 1)[-1], path, entry.kind, entry.mode,
                             entry.oid, entry.size)
    return None


def is_binary(data: bytes) -> bool:
    """Use git's own test: a NUL byte in the first 8000 bytes means binary."""
    return b"\x00" in data[:8000]


def read_blob(repo: Path, sha: str, path: str, max_bytes: int,
              info: TreeEntry | None = None) -> Blob | None:
    """Return a file with at most `max_bytes` of its content, or None when it is no file.

    `size` is always the full size. `truncated` is True when the file is
    larger than `max_bytes`.

    Args:
        repo: The bare repository folder.
        sha: A full commit sha.
        path: The file path.
        max_bytes: The largest number of bytes to read.
        info: The tree entry of the file, when the caller has it from a
            listing of the same commit. It saves one git call.
    """
    if info is None:
        info = path_info(repo, sha, path)
    if info is None or info.kind != "blob":
        return None
    code, out, truncated = _run(repo, ["cat-file", "blob", info.oid], max_bytes=max_bytes)
    if code != 0 and not truncated:
        return None
    size = info.size if info.size is not None else len(out)
    return Blob(info.path, info.oid, info.mode, size, out, truncated or size > len(out),
                is_binary(out))


def find_readme(tree: Tree) -> TreeEntry | None:
    """Return the README file of a folder listing (Markdown first), or None."""
    by_name = {e.name.lower(): e for e in reversed(tree.entries) if e.kind == "blob"}
    return next((by_name[n] for n in _README_NAMES if n in by_name), None)


def readme(repo: Path, sha: str, path: str = "", max_bytes: int = 512 * 1024) -> Blob | None:
    """Return the README of a folder, or None when there is none."""
    tree = list_tree(repo, sha, path)
    entry = find_readme(tree) if tree else None
    return read_blob(repo, sha, entry.path, max_bytes) if entry else None


# --- history ----------------------------------------------------------------

_LOG_FIELDS = ("%H", "%P", "%an", "%ae", "%at", "%cn", "%ce", "%ct", "%s")


def _parse_commits(out: bytes, with_body: bool) -> list[Commit]:
    width = len(_LOG_FIELDS) + (1 if with_body else 0)
    tokens = _text(out).split("\x00")
    commits: list[Commit] = []
    for i in range(0, len(tokens) - width + 1, width):
        f = tokens[i:i + width]
        sha = f[0].strip()
        if not is_sha(sha):
            break
        body = ""
        if with_body:
            # %B is the whole message: drop the subject paragraph.
            body = f[9].strip("\n").partition("\n\n")[2].strip("\n")
        commits.append(Commit(
            sha, tuple(f[1].split()), f[2], f[3], int(f[4] or 0),
            f[5], f[6], int(f[7] or 0), f[8], body,
        ))
    return commits


@_cached(128)
def log(repo: Path, sha: str, path: str | None = None, skip: int = 0,
        limit: int = 30) -> tuple[Commit, ...]:
    """Return one page of the history that ends at `sha`, newest first.

    Args:
        repo: The bare repository folder.
        sha: A full commit sha.
        path: Only commits that change this file or folder. Renames are not followed.
        skip: The number of commits to skip.
        limit: The page size, 1 to 500.

    Raises:
        GitError: git could not read the history.
    """
    skip = max(0, int(skip))
    limit = min(500, max(1, int(limit)))
    args = ["log", "-z", "--format=" + "%x00".join(_LOG_FIELDS),
            f"--skip={skip}", f"--max-count={limit}", _need_sha(sha)]
    if path:
        args += ["--", _need_path(path)]
    code, out, _ = _run(repo, args)
    if code != 0:
        # Not an empty result: that would stay in the cache.
        raise GitError("git log failed. The repository can be damaged or not complete.")
    return tuple(_parse_commits(out, with_body=False))


@_cached(256)
def commit(repo: Path, sha: str) -> Commit | None:
    """Return one commit with its parents and its full message, or None."""
    fmt = "%x00".join((*_LOG_FIELDS, "%B"))
    code, out, _ = _run(repo, ["log", "-z", "-1", f"--format={fmt}", _need_sha(sha)],
                        max_bytes=1024 * 1024)
    commits = _parse_commits(out, with_body=True) if code == 0 else []
    return commits[0] if commits else None


# --- diff -------------------------------------------------------------------

# Fixed options, so that the repository configuration cannot change the format
# or start an external program.
_DIFF_OPTS = ["--no-color", "--no-ext-diff", "--no-textconv", "-M",
              "--src-prefix=a/", "--dst-prefix=b/", "--submodule=short"]


def _parse_numstat(out: bytes) -> list[tuple[int | None, int | None, str, str]]:
    """Parse `--numstat -z` into (added, deleted, old path, new path)."""
    tokens = _text(out).split("\x00")
    rows: list[tuple[int | None, int | None, str, str]] = []
    i = 0
    while i < len(tokens):
        parts = tokens[i].split("\t", 2)
        if len(parts) != 3:
            i += 1
            continue
        added = int(parts[0]) if parts[0].isdigit() else None
        deleted = int(parts[1]) if parts[1].isdigit() else None
        if parts[2]:
            rows.append((added, deleted, parts[2], parts[2]))
            i += 1
        elif i + 2 < len(tokens):
            # A rename: the two paths follow as their own tokens.
            rows.append((added, deleted, tokens[i + 1], tokens[i + 2]))
            i += 3
        else:
            break
    return rows


def _parse_block(block: list[str]) -> tuple[str, bool, list[Hunk], str]:
    """Parse the lines of one file diff into (status, binary, hunks, omitted)."""
    status, binary = "modified", False
    hunks: list[Hunk] = []
    lines: list[DiffLine] = []
    header = ""
    old_no = new_no = 0
    count = 0
    in_hunks = False
    for raw in block[1:]:
        match = _HUNK_RE.match(raw) if raw.startswith("@@") else None
        if match:
            if in_hunks:
                hunks.append(Hunk(header, tuple(lines)))
            in_hunks, header, lines = True, raw[:MAX_LINE_CHARS], []
            old_no, new_no = int(match.group(1)), int(match.group(3))
            continue
        if not in_hunks:
            if raw.startswith("new file mode"):
                status = "added"
            elif raw.startswith("deleted file mode"):
                status = "deleted"
            elif raw.startswith("rename from"):
                status = "renamed"
            elif raw.startswith(("Binary files ", "GIT binary patch")):
                binary = True
            continue
        count += 1
        if count > MAX_DIFF_FILE_LINES:
            return status, binary, [], f"The diff of this file has more than " \
                                       f"{MAX_DIFF_FILE_LINES} lines."
        kind, text = raw[:1], raw[1:]
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS] + " [line cut]"
        if kind == "+":
            lines.append(DiffLine("+", None, new_no, text))
            new_no += 1
        elif kind == "-":
            lines.append(DiffLine("-", old_no, None, text))
            old_no += 1
        elif kind == "\\":
            lines.append(DiffLine("\\", None, None, text.strip()))
        else:
            lines.append(DiffLine(" ", old_no, new_no, text))
            old_no += 1
            new_no += 1
    if in_hunks:
        hunks.append(Hunk(header, tuple(lines)))
    return status, binary, hunks, ""


@_cached(16)
def diff(repo: Path, base: str | None, head: str, max_files: int = MAX_DIFF_FILES,
         max_bytes: int = MAX_DIFF_BYTES) -> Diff:
    """Return the changes from `base` to `head` as files and hunks.

    The file list with its line counts is always complete up to `max_files`.
    When the patch text is larger than `max_bytes`, or there are more files
    than `max_files`, `too_large` is True and the files that did not fit have
    no hunks and an `omitted` text.

    Args:
        repo: The bare repository folder.
        base: A full commit sha, or None to show `head` as a first commit
            (all files added).
        head: A full commit sha.
        max_files: The largest number of files to return.
        max_bytes: The largest patch text to read.
    """
    head = _need_sha(head)
    if base is None:
        cmd = ["diff-tree", "--root", "-r", "--no-commit-id", *_DIFF_OPTS]
        target = [head]
    else:
        cmd = ["diff", *_DIFF_OPTS]
        target = [_need_sha(base), head]

    code, stat_out, stat_cut = _run(repo, [*cmd, "--numstat", "-z", *target])
    if code != 0 and not stat_cut:
        raise GitError("git diff failed. One of the two commits is not in the mirror.")
    rows = _parse_numstat(stat_out)
    total = len(rows)
    too_large = stat_cut or total > max_files
    rows = rows[:max_files]

    _, patch_out, patch_cut = _run(repo, [*cmd, "-p", *target], max_bytes=max_bytes)
    too_large = too_large or patch_cut
    blocks: list[list[str]] = []
    for line in _text(patch_out).split("\n"):
        # A content line always starts with " ", "+", "-" or "\", so this
        # text at column 0 can only be a file header.
        if line.startswith("diff --git "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    for block in blocks:
        if block and block[-1] == "":
            block.pop()  # the newline that ends the block, not a context line
    if patch_cut and blocks:
        blocks.pop()  # the last block can be cut in the middle

    files: list[DiffFile] = []
    for i, (added, deleted, old_path, new_path) in enumerate(rows):
        if i < len(blocks):
            status, binary, hunks, omitted = _parse_block(blocks[i])
        else:
            status, binary, hunks = "modified", False, []
            omitted = "The diff is too large. The lines of this file are not shown."
        if old_path != new_path:
            status = "renamed"
        binary = binary or added is None
        files.append(DiffFile(old_path, new_path, status, added, deleted, binary,
                              tuple(hunks), omitted))
    return Diff(tuple(files), total,
                sum(f.added or 0 for f in files), sum(f.deleted or 0 for f in files),
                too_large)


# --- search -----------------------------------------------------------------

def grep(repo: Path, sha: str, pattern: str, limit: int = 200,
         ignore_case: bool = True) -> GrepResult:
    """Search the files of one commit for a fixed string (no regular expression).

    Args:
        repo: The bare repository folder.
        sha: A full commit sha.
        pattern: The text to find. Only its first line is used.
        limit: The largest number of matching lines to return.
        ignore_case: Do not make a difference between upper and lower case.
    """
    pattern = pattern.split("\n", 1)[0].replace("\x00", "")[:200]
    if not pattern:
        return GrepResult((), False)
    limit = min(2000, max(1, int(limit)))
    args = ["grep", "-z", "-n", "-I", "-F", "--no-color", "--full-name", "--max-count=50"]
    if ignore_case:
        args.append("-i")
    # `-e` makes the next argument a pattern, also when it starts with "-".
    args += ["-e", pattern, _need_sha(sha), "--"]
    code, out, truncated = _run(repo, args, max_bytes=1024 * 1024)
    if code not in (0, 1) and not truncated:
        return GrepResult((), False)
    prefix = f"{sha}:"
    matches: list[GrepMatch] = []
    for line in _text(out).split("\n"):
        fields = line.split("\x00", 2)
        if len(fields) != 3 or not fields[1].isdigit() or not fields[0].startswith(prefix):
            continue
        if len(matches) >= limit:
            truncated = True
            break
        matches.append(GrepMatch(fields[0][len(prefix):], int(fields[1]), fields[2][:400]))
    return GrepResult(tuple(matches), truncated)
