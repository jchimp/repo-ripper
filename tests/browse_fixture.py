"""Local git mirrors and metadata folders for the browser tests. Test fixture data only.

`build_site` makes one mirror root with these repositories:

    octo/demo      mirror + wiki mirror + metadata
    octo/gitonly   mirror only
    octo/metaonly  metadata only
    octo/nothing   no mirror, no metadata
    octo/empty     a mirror with no commits

Nothing here uses the network or the global git configuration.
"""
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.metadata import store

DEMO = "octo/demo"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(20)
MISSING_SHA = "f" * 40

README = """# Demo project

<script>alert('readme')</script>

See the [guide](docs/guide.md) and the [source](/src/app.py).
![logo](logo.png) ![badge](https://img.example.com/badge.svg)
[bad](javascript:alert(1)) and issue #2.
"""

APP_V1 = 'def greet(name):\n    """Say hello."""\n    return "hello " + name\n'
APP_V2 = APP_V1 + '\n\ndef farewell(name):\n    return "goodbye " + name\n'


@dataclass
class Site:
    """Paths and shas of the fixture."""
    root: Path                       # the mirror root
    demo: Path                       # octo/demo.git
    shas: dict[str, str] = field(default_factory=dict)


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Ann Author", "GIT_AUTHOR_EMAIL": "ann@example.com",
        "GIT_COMMITTER_NAME": "Cy Committer", "GIT_COMMITTER_EMAIL": "cy@example.com",
    })
    return env


def git(cwd: Path, *args: str, date: int | None = None) -> str:
    """Run git in `cwd` with a fixed identity and return its output."""
    env = _git_env()
    if date is not None:
        stamp = f"{1_700_000_000 + date * 3600} +0000"
        env.update({"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
    res = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, check=False)
    assert res.returncode == 0, f"git {args}: {res.stderr.decode(errors='replace')}"
    return res.stdout.decode("utf-8", errors="replace").strip()


def _write(base: Path, rel: str, data: str | bytes) -> None:
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)


def _build_source(src: Path) -> dict[str, str]:
    src.mkdir(parents=True)
    git(src, "init", "-q", "-b", "main", ".")
    git(src, "config", "core.autocrlf", "false")
    _write(src, "README.md", README)
    _write(src, "src/app.py", APP_V1)
    _write(src, "docs/guide.md", "# Guide\n\nBack to the [readme](../README.md).\n")
    _write(src, "a b.txt", "a file name with a space\n")
    _write(src, "-dash.txt", "a file name that starts with a dash\n")
    _write(src, "bin.dat", b"\x00\x01\x02binary\x00data")
    _write(src, "logo.png", PNG)
    _write(src, "fake.png", "<script>alert('not a png')</script>\n")
    _write(src, "evil.svg", '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>\n')
    _write(src, "page.html", "<html><script>alert('page')</script></html>\n")
    _write(src, "old-name.txt", "this file gets a new name\nline two\nline three\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "Initial commit", date=1)
    shas = {"first": git(src, "rev-parse", "HEAD")}

    _write(src, "src/app.py", APP_V2)
    git(src, "mv", "old-name.txt", "new-name.txt")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "Add farewell\n\nThe body of the message.\nSecond line.",
        date=2)
    shas["second"] = git(src, "rev-parse", "HEAD")
    git(src, "tag", "-a", "v1.0", "-m", "Version 1.0", date=2)

    git(src, "checkout", "-q", "-b", "feature/x")
    _write(src, "feature.txt", "needle in the feature branch\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "Add the feature file", date=3)
    shas["feature"] = git(src, "rev-parse", "HEAD")

    git(src, "checkout", "-q", "main")
    _write(src, "notes.txt", "Needle on main\nno match here\nneedle.again\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "Add notes", date=4)
    shas["main"] = git(src, "rev-parse", "HEAD")

    # A ref that looks like an option, and a GitHub pull request ref.
    git(src, "update-ref", "refs/heads/--output=x", shas["second"])
    git(src, "update-ref", "refs/pull/1/head", shas["feature"])
    return shas


def _build_wiki(src: Path) -> None:
    src.mkdir(parents=True)
    git(src, "init", "-q", "-b", "master", ".")
    _write(src, "Home.md", "# Wiki home\n\nSee [[Setup]] and [setup](Setup.md).\n")
    _write(src, "Setup.md", "# Setup\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "Write the wiki", date=1)


def _dump(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _user(login: str) -> dict:
    return {"login": login, "id": 1}


def _build_meta(base: Path, shas: dict[str, str]) -> None:
    """Write a metadata folder in the documented layout (see app/metadata/store.py)."""
    bug = {"name": "bug", "color": "d73a4a"}
    pull_1 = {
        "pull": {
            "number": 1, "title": "Add the feature <b>file</b>", "state": "closed",
            "merged": True, "merged_at": "2024-01-07T10:00:00Z", "draft": False,
            "merged_by": _user("mo"), "user": _user("cy"), "labels": [bug],
            "body": "Fixes #2.\n\n<script>alert('pr')</script>\n\n- [x] tests\n- [ ] docs",
            "created_at": "2024-01-05T10:00:00Z", "updated_at": "2024-01-07T10:00:00Z",
            "closed_at": "2024-01-07T10:00:00Z",
            "html_url": "https://github.com/octo/demo/pull/1",
            "head": {"sha": shas["feature"], "ref": "feature/x", "label": "cy:feature/x"},
            "base": {"sha": shas["main"], "ref": "main", "label": "octo:main"},
        },
        "comments": [{"id": 11, "body": "A conversation comment.", "user": _user("bob"),
                      "created_at": "2024-01-05T11:00:00Z"}],
        "reviews": [
            {"id": 21, "state": "APPROVED", "body": "Looks good.", "user": _user("dee"),
             "submitted_at": "2024-01-06T09:00:00Z"},
            {"id": 22, "state": "COMMENTED", "body": "", "user": _user("dee"),
             "submitted_at": "2024-01-06T08:00:00Z"},
        ],
        "review_comments": [{
            "id": 31, "body": "Rename this.", "path": "feature.txt", "user": _user("dee"),
            "created_at": "2024-01-06T08:00:00Z",
            "diff_hunk": "@@ -0,0 +1 @@\n+needle in the feature branch",
        }],
        "commits": [
            {"sha": shas["feature"], "commit": {"message": "Add the feature file\n\nBody",
                                                "author": {"name": "Ann Author",
                                                           "date": "2024-01-05T09:00:00Z"}},
             "author": _user("cy")},
            {"sha": MISSING_SHA, "commit": {"message": "A commit that was force-pushed away",
                                            "author": {"name": "Ann Author",
                                                       "date": "2024-01-05T09:30:00Z"}},
             "author": _user("cy")},
        ],
        "fetched_at": "2024-06-01T12:00:00Z",
    }
    pull_3 = {
        "pull": {
            "number": 3, "title": "Work in progress", "state": "open", "merged": False,
            "draft": True, "user": _user("cy"), "labels": [], "body": None,
            "created_at": "2024-02-01T10:00:00Z", "updated_at": "2024-02-01T10:00:00Z",
            "head": {"sha": MISSING_SHA, "ref": "wip"}, "base": {"sha": shas["main"],
                                                                 "ref": "main"},
        },
        "comments": [], "reviews": [], "review_comments": [], "commits": [],
        "fetched_at": "2024-06-01T12:00:00Z",
    }
    issue_2 = {
        "issue": {
            "number": 2, "title": "Crash on start", "state": "closed", "user": _user("ann"),
            "labels": [bug], "closed_at": "2024-01-08T10:00:00Z",
            "body": f"It crashes. See [x](javascript:alert(1)) and {shas['first']}.",
            "created_at": "2024-01-02T10:00:00Z", "updated_at": "2024-01-08T10:00:00Z",
        },
        "comments": [{"id": 41, "body": "Same here, see #1.", "user": _user("bob"),
                      "created_at": "2024-01-03T10:00:00Z"}],
        "fetched_at": "2024-06-01T12:00:00Z",
    }
    entries = [store.index_entry("pull", pull_1), store.index_entry("pull", pull_3),
               store.index_entry("issue", issue_2)]
    _dump(base / "pulls" / "1.json", pull_1)
    _dump(base / "pulls" / "3.json", pull_3)
    _dump(base / "issues" / "2.json", issue_2)
    for number in range(10, 15):
        doc = {"issue": {"number": number, "title": f"Open issue {number}", "state": "open",
                         "user": _user("ann"), "labels": [], "body": "text",
                         "created_at": "2024-03-01T10:00:00Z",
                         "updated_at": "2024-03-01T10:00:00Z"},
               "comments": [], "fetched_at": "2024-06-01T12:00:00Z"}
        _dump(base / "issues" / f"{number}.json", doc)
        entries.append(store.index_entry("issue", doc))
    _dump(base / "releases.json", [{
        "id": 1, "tag_name": "v1.0", "name": "First release", "body": "Notes with **bold**.",
        "published_at": "2024-01-09T10:00:00Z", "prerelease": False, "draft": False,
        "assets": [{"name": "app.zip", "size": 2048}],
    }, {"id": 2, "tag_name": "v0.9", "name": "Old one", "body": "", "assets": []}])
    _dump(base / "repo.json", {"full_name": DEMO, "description": "Demo"})
    _dump(base / "labels.json", [bug])
    _dump(base / "milestones.json", [])
    entries.sort(key=lambda e: e["number"])
    _dump(base / "index.json", {"format_version": 1, "items": entries})


_site: Site | None = None


def build_site(base: Path) -> Site:
    """Build the fixture once for the test session."""
    global _site
    if _site is not None:
        return _site
    work, root = base / "work", base / "mirrors"
    shas = _build_source(work / "demo")
    _build_wiki(work / "wiki")
    (root / "octo").mkdir(parents=True)
    demo = root / "octo" / "demo.git"
    git(base, "clone", "-q", "--mirror", str(work / "demo"), str(demo))
    git(base, "clone", "-q", "--mirror", str(work / "wiki"), str(root / "octo" / "demo.wiki.git"))
    shutil.copytree(demo, root / "octo" / "gitonly.git")
    git(base, "init", "-q", "--bare", "-b", "main", str(root / "octo" / "empty.git"))
    _build_meta(root / "octo" / "demo.meta", shas)
    _build_meta(root / "octo" / "metaonly.meta", shas)
    _site = Site(root, demo, shas)
    return _site
