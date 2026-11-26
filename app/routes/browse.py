"""Read-only browser for the mirrors and their captured metadata.

All pages are GET and sit under `/repos/{repo_id}/`. The repository folder is
always built from the database row (`repos.full_name`), never from the URL.

URL scheme for revisions: the path of the file is in the URL path, and the
revision is in the query parameter `rev` (`/repos/3/blob/src/a.py?rev=feature/x`).
A branch name can contain `/`, so a revision in the URL path would need a
guess about where the name ends. A query parameter needs no guess. With no
`rev` the page shows the default branch.

The wiki mirror (`<name>.wiki.git`) uses the same handlers under
`/repos/{repo_id}/wiki/`.
"""
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from .. import db, gitread, highlight, mdrender, runtime
from ..metadata import store
from ..web import render, templates

router = APIRouter()

PER_PAGE = 30
MAX_TEXT_VIEW = 1024 * 1024         # a larger text file is not shown in the page
MAX_README = 512 * 1024
MAX_RAW = 32 * 1024 * 1024          # a larger file is not served by the raw route
MAX_SEARCH_RESULTS = 200
MAX_REFS_SHOWN = 500
COLLAPSE_DIFF_LINES = 400           # a larger file diff starts closed
PULL_JOB = "Pull mirrors"
METADATA_JOB = "Capture metadata"

_MARKDOWN_EXT = (".md", ".markdown", ".mdown")
_HEX_RE = re.compile(r"[0-9a-f]{7,64}")
_COLOR_RE = re.compile(r"[0-9a-fA-F]{6}")
# Raster image types that a browser can show without running anything.
# extension -> (media type, test of the first bytes)
_RASTER: dict[str, tuple[str, Callable[[bytes], bool]]] = {
    ".png": ("image/png", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
    ".jpg": ("image/jpeg", lambda b: b.startswith(b"\xff\xd8\xff")),
    ".jpeg": ("image/jpeg", lambda b: b.startswith(b"\xff\xd8\xff")),
    ".gif": ("image/gif", lambda b: b.startswith((b"GIF87a", b"GIF89a"))),
    ".webp": ("image/webp", lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP"),
}
RAW_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
    "Cache-Control": "private, max-age=0",
}


# --- template filters -------------------------------------------------------

def _to_timestamp(value: object) -> float | None:
    """Accept a unix time or a GitHub ISO 8601 text."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value) if value > 0 else None
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _ago(value: object) -> str:
    """Relative time: "3 days ago"."""
    ts = _to_timestamp(value)
    if ts is None:
        return "unknown date"
    seconds = max(0, int(time.time() - ts))
    for size, unit in ((31536000, "year"), (2592000, "month"), (86400, "day"),
                       (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return "just now"


def _when(value: object) -> str:
    """Absolute local time, for the `title` of a relative time."""
    ts = _to_timestamp(value)
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def _label_color(value: object) -> str:
    """Return a GitHub label colour as `#rrggbb`, or "" when it is not one."""
    return f"#{value}" if isinstance(value, str) and _COLOR_RE.fullmatch(value) else ""


templates.env.filters["ago"] = _ago
templates.env.filters["when"] = _when
templates.env.filters["label_color"] = _label_color


# --- repository context -----------------------------------------------------

@dataclass
class Urls:
    """Builds the links of one repository (or its wiki) at one revision."""
    repo_base: str        # "/repos/3"
    base: str             # "/repos/3" or "/repos/3/wiki"
    wiki: bool
    rev: str | None       # the revision the user selected; None: default branch

    def _q(self, **params: object) -> str:
        pairs = [(k, str(v)) for k, v in params.items() if v not in (None, "", False)]
        return f"?{urlencode(pairs)}" if pairs else ""

    def _file(self, kind: str, path: str, rev: str | None, **extra: object) -> str:
        return (f"{self.base}/{kind}/{quote(path, safe='/')}"
                f"{self._q(rev=rev or self.rev, **extra)}")

    def code(self, rev: str | None = None) -> str:
        root = self.base if self.wiki else f"{self.base}/code"
        return f"{root}{self._q(rev=rev or self.rev)}"

    def tree(self, path: str = "", rev: str | None = None) -> str:
        return self._file("tree", path, rev) if path else self.code(rev)

    def blob(self, path: str, rev: str | None = None, line: int | None = None,
             plain: bool = False) -> str:
        url = self._file("blob", path, rev, plain="1" if plain else "")
        return f"{url}#L{line}" if line else url

    def raw(self, path: str, rev: str | None = None) -> str:
        return self._file("raw", path, rev)

    def commits(self, path: str = "", rev: str | None = None, page: int = 1) -> str:
        tail = f"/{quote(path, safe='/')}" if path else ""
        return (f"{self.base}/commits{tail}"
                f"{self._q(rev=rev or self.rev, page=page if page > 1 else None)}")

    def commit(self, sha: str) -> str:
        return f"{self.base}/commit/{quote(sha, safe='')}"

    def search(self, **params: object) -> str:
        return f"{self.repo_base}/search{self._q(**params)}"

    def page(self, area: str, **params: object) -> str:
        """A metadata page: `area` is "pulls", "issues/12", "releases", ..."""
        return f"{self.repo_base}/{area}{self._q(**params)}"


@dataclass
class RepoCtx:
    """All a handler needs to know about the repository of the request."""
    row: Any
    repo_id: int
    full_name: str
    wiki: bool
    git_dir: Path          # the mirror this request reads (code or wiki)
    has_mirror: bool
    has_wiki: bool
    has_meta: bool
    urls: Urls

    @property
    def has_git(self) -> bool:
        return self.has_wiki if self.wiki else self.has_mirror


_meta_cache: dict[str, tuple[tuple, dict, dict[int, str]]] = {}


def _stat_key(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def _meta_info(full_name: str) -> tuple[dict, dict[int, str]]:
    """Return (`store.summary`, number -> kind), read again only after a change.

    index.json can be large and lives on a NAS, and every page needs the
    counts for the tabs.
    """
    base = store.meta_dir(full_name)
    key = (_stat_key(base / "index.json"), _stat_key(base / "releases.json"))
    hit = _meta_cache.get(full_name)
    if hit and hit[0] == key:
        return hit[1], hit[2]
    numbers = {e["number"]: e.get("kind") or "issue" for e in store.load_index(full_name)
               if isinstance(e.get("number"), int)}
    summary = store.summary(full_name)
    _meta_cache[full_name] = (key, summary, numbers)
    return summary, numbers


def _mirror_dirs(full_name: str) -> tuple[Path, Path]:
    """Return the code mirror and the wiki mirror folder of a repository."""
    root = runtime.mirror_root().resolve()
    main = (root / f"{store.validate_full_name(full_name)}.git").resolve()
    wiki = (root / f"{full_name}.wiki.git").resolve()
    if not main.is_relative_to(root) or not wiki.is_relative_to(root):
        raise ValueError(f"Invalid repository name: {full_name!r}")
    return main, wiki


def repo_tabs(row: Any, active: str = "") -> dict:
    """Return the data for `_repo_tabs.html`.

    Other pages (for example the repository detail page) can call this and
    pass the result to the template as `tabs`.

    Args:
        row: A row of the `repos` table.
        active: The key of the current tab: "overview", "code", "commits",
            "pulls", "issues", "releases" or "wiki".
    """
    try:
        main, wiki = _mirror_dirs(row["full_name"])
        has_meta = store.has_metadata(row["full_name"])
    except ValueError:
        return {"repo_id": row["id"], "active": active, "has_mirror": False,
                "has_wiki": False, "has_meta": False, "pulls": 0, "issues": 0, "releases": 0}
    summary = _meta_info(row["full_name"])[0] if has_meta else None
    return {
        "repo_id": row["id"], "active": active,
        "has_mirror": gitread.repo_exists(main),
        "has_wiki": gitread.repo_exists(wiki),
        "has_meta": has_meta,
        "pulls": summary["pulls"]["total"] if summary else 0,
        "issues": summary["issues"]["total"] if summary else 0,
        "releases": summary["releases"] if summary else 0,
    }


def _load(request: Request, repo_id: int, rev: str | None = None) -> RepoCtx:
    row = db.get_repo(repo_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Repository not found.")
    full_name = row["full_name"]
    try:
        main, wiki_dir = _mirror_dirs(full_name)
        has_meta = store.has_metadata(full_name)
    except ValueError:
        raise HTTPException(status_code=404, detail="Repository not found.") from None
    # ".../repos/3/wiki/..." selects the wiki mirror.
    parts = request.url.path.split("/")
    wiki = len(parts) > 3 and parts[3] == "wiki"
    repo_base = f"/repos/{repo_id}"
    urls = Urls(repo_base, f"{repo_base}/wiki" if wiki else repo_base, wiki, rev or None)
    return RepoCtx(row, repo_id, full_name, wiki, wiki_dir if wiki else main,
                   gitread.repo_exists(main), gitread.repo_exists(wiki_dir), has_meta, urls)


def _page(request: Request, rc: RepoCtx, template: str, active: str,
          ctx: dict | None = None, status_code: int = 200) -> Response:
    data = {
        "active": "repos", "repo": rc.row, "rc": rc, "u": rc.urls,
        "tabs": repo_tabs(rc.row, "wiki" if rc.wiki else active),
        **(ctx or {}),
    }
    return render(request, template, data, status_code=status_code)


def _state(request: Request, rc: RepoCtx, active: str, state: str, message: str = "",
           status_code: int = 200) -> Response:
    """Show an honest "there is nothing here, and this is why" page."""
    return _page(request, rc, "browse_state.html", active,
                 {"state": state, "message": message,
                  "pull_job": PULL_JOB, "metadata_job": METADATA_JOB}, status_code)


def _guard(func: Callable) -> Callable:
    """Turn a git failure into a page with a message the user can act on."""
    @wraps(func)
    def wrapper(request: Request, repo_id: int, *args: Any, **kwargs: Any) -> Response:
        try:
            return func(request, repo_id, *args, **kwargs)
        except gitread.GitError as exc:
            rc = _load(request, repo_id)
            return _state(request, rc, "code", "git_error", str(exc), 500)
    return wrapper


def _both(path: str) -> Callable:
    """Register a handler for the code mirror and for the wiki mirror."""
    def decorate(func: Callable) -> Callable:
        router.get(f"/repos/{{repo_id}}/wiki{path}")(func)
        router.get(f"/repos/{{repo_id}}{path}")(func)
        return func
    return decorate


@dataclass
class _Rev:
    name: str      # what to show: branch or tag name, or a short sha
    sha: str


def _resolve_rev(rc: RepoCtx, rev: str | None,
                 refs: gitread.Refs | None = None) -> _Rev | None:
    """Resolve `rev` (or the default branch). None: the repository has no commits."""
    name = rev
    if not name:
        refs = refs or gitread.list_refs(rc.git_dir)
        name = refs.default_branch
        if name is None:
            return None
    sha = gitread.resolve(rc.git_dir, name)
    if sha is None:
        if rev:
            raise _NotFound(f"The revision {rev[:80]!r} is not in this mirror.")
        return None
    shown = name[:10] if gitread.is_sha(name) else name
    return _Rev(shown, sha)


class _NotFound(Exception):
    """The repository exists, but the revision, path or item does not."""


def _handles_not_found(func: Callable) -> Callable:
    @wraps(func)
    def wrapper(request: Request, repo_id: int, *args: Any, **kwargs: Any) -> Response:
        try:
            return func(request, repo_id, *args, **kwargs)
        except _NotFound as exc:
            rc = _load(request, repo_id)
            return _state(request, rc, "code", "not_found", str(exc), 404)
    return wrapper


def _git_page(func: Callable) -> Callable:
    return _guard(_handles_not_found(func))


def _path_or_404(path: str) -> str:
    cleaned = gitread.clean_path(path)
    if cleaned is None:
        raise _NotFound("This path is not valid.")
    return cleaned


def _crumbs(path: str) -> list[tuple[str, str]]:
    """Return (name, path) for each part of a path."""
    parts = path.split("/") if path else []
    return [(part, "/".join(parts[:i + 1])) for i, part in enumerate(parts)]


def _link_ctx(rc: RepoCtx, rev: str = "", directory: str = "",
              files: bool = False) -> mdrender.LinkContext:
    numbers = _meta_info(rc.full_name)[1] if rc.has_meta else {}
    return mdrender.LinkContext(
        repo_base=rc.urls.repo_base,
        code_base=rc.urls.base if files else "",
        rev=rev, directory=directory, numbers=numbers,
        link_commits=rc.has_mirror,
    )


def _last_commit(rc: RepoCtx, sha: str, path: str) -> gitread.Commit | None:
    """Return the newest commit that changed `path`, or None.

    This line is an extra on the page. In a very large history the search for
    it can reach the time limit; then the page shows without it.
    """
    try:
        found = gitread.log(rc.git_dir, sha, path or None, 0, 1)
    except gitread.GitError:
        return None
    return found[0] if found else None


def _raster_type(name: str, head: bytes) -> str | None:
    """Return the media type when the name and the first bytes are a raster image."""
    for ext, (media_type, test) in _RASTER.items():
        if name.lower().endswith(ext) and test(head):
            return media_type
    return None


def _is_raster(rc: RepoCtx, sha: str, info: gitread.TreeEntry) -> bool:
    """Return True when the file is a raster image by its name and its first bytes."""
    if not info.name.lower().endswith(tuple(_RASTER)):
        return False
    head = gitread.read_blob(rc.git_dir, sha, info.path, 16, info=info)
    return head is not None and _raster_type(info.name, head.data) is not None


# --- code: tree, blob, raw --------------------------------------------------

def _tree_page(request: Request, repo_id: int, path: str, rev: str | None) -> Response:
    rc = _load(request, repo_id, rev)
    active = "code"
    if not rc.has_git:
        return _state(request, rc, active, "no_wiki" if rc.wiki else "no_mirror")
    refs = gitread.list_refs(rc.git_dir)
    resolved = _resolve_rev(rc, rev, refs)
    if resolved is None:
        return _state(request, rc, active, "empty_repo")
    path = _path_or_404(path)
    tree = gitread.list_tree(rc.git_dir, resolved.sha, path)
    if tree is None:
        info = gitread.path_info(rc.git_dir, resolved.sha, path)
        if info and info.kind == "blob":
            return RedirectResponse(rc.urls.blob(path), status_code=307)
        raise _NotFound(f"The folder {path[:200]!r} is not in revision {resolved.name}.")
    last = _last_commit(rc, resolved.sha, path)

    readme_entry = gitread.find_readme(tree)
    if readme_entry is None and rc.wiki and not path:
        # The start page of a GitHub wiki is Home.md.
        readme_entry = next((e for e in tree.entries
                             if e.kind == "blob" and e.name.lower() == "home.md"), None)
    readme_html = readme_text = None
    readme_note = ""
    if readme_entry:
        if (readme_entry.size or 0) > MAX_README:
            readme_note = "The README file is too large to show here. Open the file."
        else:
            blob = gitread.read_blob(rc.git_dir, resolved.sha, readme_entry.path, MAX_README,
                                     info=readme_entry)
            if blob and not blob.binary:
                if readme_entry.name.lower().endswith(_MARKDOWN_EXT):
                    # Links in the README stay on the revision of this page.
                    readme_html = mdrender.render(blob.text(), _link_ctx(
                        rc, rev=rc.urls.rev or "", directory=path, files=True))
                else:
                    readme_text = blob.text()
    return _page(request, rc, "browse_tree.html", active, {
        "rev": resolved, "refs": refs, "path": path, "crumbs": _crumbs(path),
        "tree": tree, "last": last,
        "readme_entry": readme_entry, "readme_html": readme_html,
        "readme_text": readme_text, "readme_note": readme_note,
        "max_refs": MAX_REFS_SHOWN,
    })


@router.get("/repos/{repo_id}/wiki")
@_git_page
def wiki_root(request: Request, repo_id: int, rev: str | None = None) -> Response:
    return _tree_page(request, repo_id, "", rev)


@router.get("/repos/{repo_id}/code")
@_git_page
def code_root(request: Request, repo_id: int, rev: str | None = None) -> Response:
    return _tree_page(request, repo_id, "", rev)


@_both("/tree/{path:path}")
@_git_page
def tree_page(request: Request, repo_id: int, path: str, rev: str | None = None) -> Response:
    return _tree_page(request, repo_id, path, rev)


@_both("/blob/{path:path}")
@_git_page
def blob_page(request: Request, repo_id: int, path: str, rev: str | None = None,
              plain: str | None = None) -> Response:
    rc = _load(request, repo_id, rev)
    if not rc.has_git:
        return _state(request, rc, "code", "no_wiki" if rc.wiki else "no_mirror")
    resolved = _resolve_rev(rc, rev)
    if resolved is None:
        return _state(request, rc, "code", "empty_repo")
    path = _path_or_404(path)
    info = gitread.path_info(rc.git_dir, resolved.sha, path) if path else None
    if info is None:
        if path == "":
            return RedirectResponse(rc.urls.code(), status_code=307)
        raise _NotFound(f"The file {path[:200]!r} is not in revision {resolved.name}.")
    if info.kind == "tree":
        return RedirectResponse(rc.urls.tree(path), status_code=307)

    ctx: dict[str, Any] = {
        "rev": resolved, "path": path, "crumbs": _crumbs(path), "info": info,
        "view": "none", "note": "", "lines": [], "highlighted": False,
        "html": None, "is_markdown": False, "symlink": info.mode == "120000",
    }
    size = info.size or 0
    if info.kind != "blob":
        ctx["view"] = "submodule"
    elif size <= MAX_RAW and _is_raster(rc, resolved.sha, info):
        # The browser gets the image from the raw route, so only the first
        # bytes are read here.
        ctx["view"] = "image"
    elif size > MAX_TEXT_VIEW:
        ctx["note"] = "The file is too large to show here. Use the Raw link."
    else:
        blob = gitread.read_blob(rc.git_dir, resolved.sha, path, MAX_TEXT_VIEW, info=info)
        if blob is None:
            raise _NotFound(f"The file {path[:200]!r} could not be read.")
        if blob.binary:
            ctx["note"] = "This is a binary file. Use the Raw link to download it."
        else:
            text = blob.text()
            ctx["is_markdown"] = path.lower().endswith(_MARKDOWN_EXT)
            if ctx["is_markdown"] and not plain:
                ctx["view"] = "markdown"
                ctx["html"] = mdrender.render(text, _link_ctx(
                    rc, rev=rc.urls.rev or "", directory=path.rpartition("/")[0], files=True))
            else:
                ctx["view"] = "text"
                colored = highlight.highlight_lines(info.name, text)
                ctx["highlighted"] = colored is not None
                ctx["lines"] = colored if colored is not None else highlight.split_lines(text)
    ctx["last"] = _last_commit(rc, resolved.sha, path)
    return _page(request, rc, "browse_blob.html", "code", ctx)


@_both("/raw/{path:path}")
def raw_file(request: Request, repo_id: int, path: str, rev: str | None = None) -> Response:
    """Serve the bytes of a file in a form that a browser will not execute."""
    def refuse(status: int, text: str) -> Response:
        return PlainTextResponse(text, status_code=status, headers=RAW_HEADERS)

    rc = _load(request, repo_id, rev)
    cleaned = gitread.clean_path(path)
    if not rc.has_git or not cleaned:
        return refuse(404, "File not found.")
    try:
        resolved = _resolve_rev(rc, rev)
        blob = (gitread.read_blob(rc.git_dir, resolved.sha, cleaned, MAX_RAW)
                if isinstance(resolved, _Rev) else None)
    except _NotFound:
        blob = None
    except gitread.GitError as exc:
        return refuse(500, str(exc))
    if blob is None:
        return refuse(404, "File not found.")
    if blob.truncated:
        limit = MAX_RAW // (1024 * 1024)
        return refuse(413, f"The file is larger than {limit} MB. This page does not serve it. "
                           f"Use git to read it from the mirror.")
    name = cleaned.rsplit("/", 1)[-1]
    image_type = _raster_type(name, blob.data[:16])
    if image_type:
        media_type, disposition = image_type, "inline"
    elif not blob.binary:
        # All text is text/plain, also HTML and SVG: the browser shows the
        # source and does not run it.
        media_type, disposition = "text/plain; charset=utf-8", "inline"
    else:
        media_type, disposition = "application/octet-stream", "attachment"
    headers = {
        **RAW_HEADERS,
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name, safe='')}",
    }
    return Response(content=blob.data, media_type=media_type, headers=headers)


# --- history ----------------------------------------------------------------

def _commits_page(request: Request, repo_id: int, path: str, rev: str | None,
                  page: int) -> Response:
    rc = _load(request, repo_id, rev)
    if not rc.has_git:
        return _state(request, rc, "commits", "no_wiki" if rc.wiki else "no_mirror")
    refs = gitread.list_refs(rc.git_dir)
    resolved = _resolve_rev(rc, rev, refs)
    if resolved is None:
        return _state(request, rc, "commits", "empty_repo")
    path = _path_or_404(path)
    page = max(1, page)
    found = gitread.log(rc.git_dir, resolved.sha, path or None,
                        (page - 1) * PER_PAGE, PER_PAGE + 1)
    return _page(request, rc, "browse_commits.html", "commits", {
        "rev": resolved, "refs": refs, "path": path, "crumbs": _crumbs(path),
        "commits": found[:PER_PAGE], "page": page, "has_next": len(found) > PER_PAGE,
        "max_refs": MAX_REFS_SHOWN,
    })


@_both("/commits")
@_git_page
def commits_page(request: Request, repo_id: int, rev: str | None = None,
                 page: int = 1) -> Response:
    return _commits_page(request, repo_id, "", rev, page)


@_both("/commits/{path:path}")
@_git_page
def commits_path_page(request: Request, repo_id: int, path: str, rev: str | None = None,
                      page: int = 1) -> Response:
    return _commits_page(request, repo_id, path, rev, page)


@_both("/commit/{sha}")
@_git_page
def commit_page(request: Request, repo_id: int, sha: str) -> Response:
    rc = _load(request, repo_id)
    if not rc.has_git:
        return _state(request, rc, "commits", "no_wiki" if rc.wiki else "no_mirror")
    full = gitread.resolve(rc.git_dir, sha) if _HEX_RE.fullmatch(sha) else None
    info = gitread.commit(rc.git_dir, full) if full else None
    if info is None:
        raise _NotFound(f"The commit {sha[:64]!r} is not in this mirror. If it is new, "
                        f"run the job {PULL_JOB}.")
    base = info.parents[0] if info.parents else None
    changes = gitread.diff(rc.git_dir, base, info.sha)
    return _page(request, rc, "browse_commit.html", "commits", {
        "commit": info, "diff": changes, "diff_rev": info.sha,
        "collapse_at": COLLAPSE_DIFF_LINES,
    })


@router.get("/repos/{repo_id}/branches")
@_git_page
def refs_page(request: Request, repo_id: int) -> Response:
    rc = _load(request, repo_id)
    if not rc.has_mirror:
        return _state(request, rc, "code", "no_mirror")
    refs = gitread.list_refs(rc.git_dir, count_pulls=True)
    releases = {}
    if rc.has_meta:
        releases = {r.get("tag_name"): r for r in store.load_releases(rc.full_name)
                    if isinstance(r, dict) and r.get("tag_name")}
    return _page(request, rc, "browse_refs.html", "code", {
        "refs": refs, "releases": releases, "max_refs": MAX_REFS_SHOWN,
    })


@router.get("/repos/{repo_id}/search")
@_git_page
def search_page(request: Request, repo_id: int, q: str = "", rev: str | None = None,
                case: str | None = None) -> Response:
    rc = _load(request, repo_id, rev)
    if not rc.has_mirror:
        return _state(request, rc, "code", "no_mirror")
    refs = gitread.list_refs(rc.git_dir)
    resolved = _resolve_rev(rc, rev, refs)
    if resolved is None:
        return _state(request, rc, "code", "empty_repo")
    query = q.strip()[:200]
    result = None
    groups: list[tuple[str, list[gitread.GrepMatch]]] = []
    if query:
        result = gitread.grep(rc.git_dir, resolved.sha, query, MAX_SEARCH_RESULTS,
                              ignore_case=not case)
        for match in result.matches:
            if groups and groups[-1][0] == match.path:
                groups[-1][1].append(match)
            else:
                groups.append((match.path, [match]))
    return _page(request, rc, "browse_search.html", "code", {
        "rev": resolved, "refs": refs, "q": query, "case": bool(case),
        "result": result, "groups": groups, "max_refs": MAX_REFS_SHOWN,
    })


# --- metadata: pull requests, issues, releases ------------------------------

def _items_page(request: Request, repo_id: int, kind: str, state: str, q: str,
                page: int) -> Response:
    rc = _load(request, repo_id)
    area = "pulls" if kind == store.KIND_PULL else "issues"
    if not rc.has_meta:
        return _state(request, rc, area, "no_meta")
    states = ("all", "open", "closed", "merged") if kind == store.KIND_PULL \
        else ("all", "open", "closed")
    if state not in states:
        state = "all"
    page = max(1, page)
    items, total = store.list_items(rc.full_name, kind, None if state == "all" else state,
                                    q.strip()[:200] or None, page, PER_PAGE)
    counts = _meta_info(rc.full_name)[0][area]
    return _page(request, rc, "browse_items.html", area, {
        "kind": kind, "area": area, "items": items, "total": total, "state": state,
        "states": states, "counts": counts, "q": q.strip()[:200], "page": page,
        "pages": max(1, -(-total // PER_PAGE)),
    })


@router.get("/repos/{repo_id}/pulls")
def pulls_page(request: Request, repo_id: int, state: str = "all", q: str = "",
               page: int = 1) -> Response:
    return _items_page(request, repo_id, store.KIND_PULL, state, q, page)


@router.get("/repos/{repo_id}/issues")
def issues_page(request: Request, repo_id: int, state: str = "all", q: str = "",
                page: int = 1) -> Response:
    return _items_page(request, repo_id, store.KIND_ISSUE, state, q, page)


def _login(obj: object) -> str:
    user = obj.get("user") if isinstance(obj, dict) else None
    return (user.get("login") if isinstance(user, dict) else None) or "unknown"


def _hunk_lines(hunk: object, keep: int = 8) -> list[tuple[str, str]]:
    """Return the last lines of a GitHub `diff_hunk` as (kind, text)."""
    if not isinstance(hunk, str):
        return []
    lines = hunk.split("\n")
    header = [ln for ln in lines[:1] if ln.startswith("@@")]
    body = lines[len(header):][-keep:]
    return [("@" if ln.startswith("@@") else ln[:1] if ln[:1] in "+-" else " ", ln)
            for ln in header + body]


def _timeline(rc: RepoCtx, doc: dict, obj: dict, md_ctx: mdrender.LinkContext) -> list[dict]:
    """Build one list of events in time order from the stored lists."""
    events: list[dict] = []

    def add(kind: str, when: object, item: dict, **extra: object) -> None:
        events.append({"kind": kind, "when": when if isinstance(when, str) else "",
                       "user": _login(item), "url": item.get("html_url"),
                       "html": mdrender.render(item.get("body"), md_ctx), **extra})

    for item in doc.get("comments") or []:
        if isinstance(item, dict):
            add("comment", item.get("created_at"), item)
    for item in doc.get("reviews") or []:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "").upper()
        # A review with no text and no verdict only holds its line comments.
        if state in ("COMMENTED", "PENDING", "") and not item.get("body"):
            continue
        add("review", item.get("submitted_at"), item, state=state)
    for item in doc.get("review_comments") or []:
        if isinstance(item, dict):
            add("review_comment", item.get("created_at"), item,
                path=item.get("path") or "", hunk=_hunk_lines(item.get("diff_hunk")))
    if obj.get("merged_at"):
        events.append({"kind": "merged", "when": obj["merged_at"],
                       "user": _login({"user": obj.get("merged_by")}), "html": ""})
    elif obj.get("closed_at"):
        events.append({"kind": "closed", "when": obj["closed_at"],
                       "user": _login({"user": obj.get("closed_by")}), "html": ""})
    # Events with no date keep their stored order and go last.
    events.sort(key=lambda e: (e["when"] == "", e["when"]))
    return events


def _badge(obj: dict, is_pull: bool) -> tuple[str, str]:
    """Return (text, css class) of the state badge."""
    if is_pull and (obj.get("merged") or obj.get("merged_at")):
        return "merged", "merged"
    if obj.get("state") == "closed":
        return "closed", "closed"
    if is_pull and obj.get("draft"):
        return "draft", "draft"
    return "open", "open"


def _sha_of(side: object) -> str:
    sha = side.get("sha") if isinstance(side, dict) else None
    return sha if gitread.is_sha(sha) else ""


@router.get("/repos/{repo_id}/pulls/{number}")
@_guard
def pull_page(request: Request, repo_id: int, number: int, tab: str = "conversation") -> Response:
    rc = _load(request, repo_id)
    if not rc.has_meta:
        return _state(request, rc, "pulls", "no_meta")
    doc = store.load_pull(rc.full_name, number) if number > 0 else None
    pull = doc.get("pull") if doc else None
    if not isinstance(pull, dict):
        return _state(request, rc, "pulls", "not_found",
                      f"Pull request #{number} is not in the captured metadata.", 404)
    if tab not in ("conversation", "commits", "files"):
        tab = "conversation"
    md_ctx = _link_ctx(rc)
    base, head = pull.get("base") or {}, pull.get("head") or {}
    ctx: dict[str, Any] = {
        "pull": pull, "doc": doc, "number": number, "tab": tab, "badge": _badge(pull, True),
        "author": _login(pull), "base": base, "head": head,
        "commit_count": len(doc.get("commits") or []),
        "collapse_at": COLLAPSE_DIFF_LINES, "pull_job": PULL_JOB,
    }
    if tab == "conversation":
        ctx["body_html"] = mdrender.render(pull.get("body"), md_ctx)
        ctx["events"] = _timeline(rc, doc, pull, md_ctx)
    elif tab == "commits":
        commits = [c for c in doc.get("commits") or [] if isinstance(c, dict)]
        shas = [c.get("sha") for c in commits]
        ctx["commits"] = commits
        ctx["known"] = gitread.commits_exist(rc.git_dir, shas) if rc.has_mirror else set()
    else:
        ctx.update(_pull_files(rc, _sha_of(base), _sha_of(head)))
    return _page(request, rc, "browse_pull.html", "pulls", ctx)


def _pull_files(rc: RepoCtx, base_sha: str, head_sha: str) -> dict:
    """Compute the "Files changed" diff from the local mirror."""
    if not rc.has_mirror:
        return {"diff": None, "files_note": "no_mirror"}
    known = gitread.commits_exist(rc.git_dir, [base_sha, head_sha])
    missing = [name for name, sha in (("base", base_sha), ("head", head_sha))
               if not sha or sha not in known]
    if missing:
        return {"diff": None, "files_note": "missing", "missing": missing,
                "base_sha": base_sha, "head_sha": head_sha}
    # The changes of the pull request alone: from the point where the head
    # left the base branch, not from the current tip of the base branch.
    start = gitread.merge_base(rc.git_dir, base_sha, head_sha) or base_sha
    return {"diff": gitread.diff(rc.git_dir, start, head_sha), "files_note": "",
            "diff_rev": head_sha, "diff_base": start}


@router.get("/repos/{repo_id}/issues/{number}")
def issue_page(request: Request, repo_id: int, number: int) -> Response:
    rc = _load(request, repo_id)
    if not rc.has_meta:
        return _state(request, rc, "issues", "no_meta")
    doc = store.load_issue(rc.full_name, number) if number > 0 else None
    issue = doc.get("issue") if doc else None
    if not isinstance(issue, dict):
        if number > 0 and store.load_pull(rc.full_name, number):
            return RedirectResponse(rc.urls.page(f"pulls/{number}"), status_code=307)
        return _state(request, rc, "issues", "not_found",
                      f"Issue #{number} is not in the captured metadata.", 404)
    md_ctx = _link_ctx(rc)
    return _page(request, rc, "browse_issue.html", "issues", {
        "issue": issue, "doc": doc, "number": number, "badge": _badge(issue, False),
        "author": _login(issue), "body_html": mdrender.render(issue.get("body"), md_ctx),
        "events": _timeline(rc, doc, issue, md_ctx),
    })


@router.get("/repos/{repo_id}/releases")
@_guard
def releases_page(request: Request, repo_id: int) -> Response:
    rc = _load(request, repo_id)
    if not rc.has_meta:
        return _state(request, rc, "releases", "no_meta")
    md_ctx = _link_ctx(rc)
    tags = {t.name for t in gitread.list_refs(rc.git_dir).tags} if rc.has_mirror else set()
    releases = []
    for rel in store.load_releases(rc.full_name):
        if not isinstance(rel, dict):
            continue
        releases.append({
            "rel": rel, "html": mdrender.render(rel.get("body"), md_ctx),
            "tag_local": rel.get("tag_name") in tags,
            "assets": [a for a in rel.get("assets") or [] if isinstance(a, dict)],
        })
    return _page(request, rc, "browse_releases.html", "releases", {"releases": releases})
