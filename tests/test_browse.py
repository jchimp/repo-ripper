"""The browser pages, on local mirrors and metadata folders."""
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import db, gitread
from app.routes import browse

from .browse_fixture import MISSING_SHA, Site, build_site


@pytest.fixture(scope="session")
def site(tmp_path_factory: pytest.TempPathFactory) -> Site:
    return build_site(tmp_path_factory.mktemp("browse"))

NAMES = ("demo", "gitonly", "metaonly", "nothing", "empty")


@pytest.fixture
def web(authed: tuple[TestClient, str], site: Site,
        monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, dict[str, int]]]:
    """A signed-in client on the fixture mirror root, plus name -> repository id."""
    client, _ = authed
    monkeypatch.setenv("MIRROR_ROOT", str(site.root))
    from app.config import get_settings
    get_settings.cache_clear()
    browse._meta_cache.clear()
    ids: dict[str, int] = {}
    for name in NAMES:
        full = f"octo/{name}"
        db.upsert_repo({"full_name": full, "private": 0, "fork": 0, "archived": 0,
                        "default_branch": "main", "mirror_path": str(site.root / f"{full}.git"),
                        "discovered_at": 0})
    for row in db.list_repos():
        ids[row["full_name"].split("/")[1]] = row["id"]
    yield client, ids
    get_settings.cache_clear()


def page(client: TestClient, url: str, status: int = 200) -> str:
    resp = client.get(url)
    assert resp.status_code == status, f"{url}: {resp.status_code}\n{resp.text[:600]}"
    return resp.text


def tab_names(html: str) -> list[str]:
    nav = re.search(r'<nav class="repo-tabs".*?</nav>', html, re.S).group(0)
    return [re.sub(r"\s*<span.*", "", m, flags=re.S).strip()
            for m in re.findall(r"<a [^>]*>(.*?)</a>", nav, re.S)]


# --- access -----------------------------------------------------------------

def test_all_pages_need_a_sign_in(client: TestClient) -> None:
    for path in ("code", "tree/src", "blob/README.md", "raw/README.md", "commits",
                 "commits/src", f"commit/{'a' * 40}", "branches", "search?q=x", "pulls",
                 "pulls/1", "issues", "issues/2", "releases", "wiki", "wiki/blob/Home.md",
                 "wiki/raw/Home.md"):
        resp = client.get(f"/repos/1/{path}")
        assert resp.status_code == 303 and resp.headers["location"] == "/login", path


def test_unknown_repository_is_404(web) -> None:
    client, _ = web
    for path in ("code", "pulls", "raw/README.md", "commit/" + "a" * 40):
        assert client.get(f"/repos/9999/{path}").status_code == 404


def test_repository_page_links_to_the_browser(web) -> None:
    """The table row opens the repository page; its tabs open the browser."""
    client, ids = web
    base = f"/repos/{ids['demo']}"
    assert f'href="{base}"' in page(client, "/repos")
    html = page(client, base)
    assert tab_names(html) == ["Overview", "Code", "Commits", "Pull requests", "Issues",
                               "Releases", "Wiki"]
    assert f'href="{base}/code"' in html


# --- code -------------------------------------------------------------------

def test_code_page(web, site: Site) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    html = page(client, f"{base}/code")
    assert tab_names(html) == ["Overview", "Code", "Commits", "Pull requests", "Issues",
                               "Releases", "Wiki"]
    assert re.search(r'Pull requests <span class="count">2</span>', html)
    assert re.search(r'Issues <span class="count">6</span>', html)
    # Tree: folders and files, with links that keep special names intact.
    assert f'href="{base}/tree/src"' in html and f'href="{base}/blob/a%20b.txt"' in html
    assert f'href="{base}/blob/-dash.txt"' in html
    # Ref picker and last commit.
    assert '<option value="feature/x" >feature/x</option>' in html
    assert '<option value="--output=x"' in html and '<option value="v1.0"' in html
    assert "Add notes" in html and site.shas["main"][:8] in html
    # README: rendered, safe, with local links.
    assert "<h1>Demo project</h1>" in html
    assert "<script>alert('readme')" not in html and "&lt;script&gt;" in html
    assert f'href="{base}/blob/docs/guide.md"' in html and f'href="{base}/blob/src/app.py"' in html
    assert f'<img src="{base}/raw/logo.png" alt="logo"' in html
    assert 'src="https://img.example.com' not in html and "javascript:" not in html.replace(
        "[bad](javascript:alert(1))", "")
    assert f'href="{base}/issues/2">#2</a>' in html


def test_tree_at_a_branch_with_a_slash(web) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    html = page(client, f"{base}/code?rev=feature/x")
    assert "feature.txt" in html and "notes.txt" not in html
    assert '<option value="feature/x" selected>' in html
    # Links stay on the branch.
    assert f'href="{base}/blob/feature.txt?rev=feature%2Fx"' in html
    assert f'href="{base}/blob/docs/guide.md?rev=feature%2Fx"' in html

    sub = page(client, f"{base}/tree/src?rev=feature%2Fx")
    assert f'href="{base}/blob/src/app.py?rev=feature%2Fx"' in sub
    assert 'aria-label="Parent folder"' in sub


def test_tree_at_a_tag_a_sha_and_an_option_like_ref(web, site: Site) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    assert "notes.txt" not in page(client, f"{base}/code?rev=v1.0")
    assert "notes.txt" not in page(client, f"{base}/code?rev=--output%3Dx")
    html = page(client, f"{base}/code?rev={site.shas['first']}")
    assert "old-name.txt" in html
    first = site.shas["first"]
    assert f'<option value="{first}" selected>{first[:10]}</option>' in html


def test_hostile_revision_is_not_found(web) -> None:
    client, ids = web
    for rev in ("--help", "main..feature/x", "HEAD~1", "main:README.md", "no-such",
                "--upload-pack=x", "main%00"):
        html = page(client, f"/repos/{ids['demo']}/code?rev={rev}", 404)
        assert "is not in this mirror" in html
        assert client.get(f"/repos/{ids['demo']}/raw/README.md?rev={rev}").status_code == 404
        page(client, f"/repos/{ids['demo']}/blob/README.md?rev={rev}", 404)
        page(client, f"/repos/{ids['demo']}/commits?rev={rev}", 404)
        page(client, f"/repos/{ids['demo']}/search?q=x&rev={rev}", 404)


def test_bad_paths_are_not_found(web) -> None:
    client, ids = web
    for path in ("tree/%2E%2E/x", "tree/src/%2E%2E/%2E%2E", "blob/%2E%2E%2Fetc",
                 "commits/%2E%2E/x", "tree/missing", "blob/missing.txt", "blob/src/app.py/x"):
        page(client, f"/repos/{ids['demo']}/{path}", 404)


def test_tree_and_blob_redirect_to_each_other(web) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    resp = client.get(f"{base}/tree/README.md")
    assert (resp.status_code, resp.headers["location"]) == (307, f"{base}/blob/README.md")
    resp = client.get(f"{base}/blob/src?rev=feature/x")
    assert (resp.status_code, resp.headers["location"]) == (307, f"{base}/tree/src?rev=feature%2Fx")


def test_blob_text_view(web) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    html = page(client, f"{base}/blob/src/app.py")
    assert '<tr id="L1"><td class="ln"><a href="#L1">1</a>' in html and 'id="L7"' in html
    assert '<span class="k">def</span>' in html  # highlighted
    assert "7 lines" in html and f'href="{base}/raw/src/app.py"' in html
    assert f'href="{base}/commits/src/app.py"' in html and "Add farewell" in html

    assert "a file name with a space" in page(client, f"{base}/blob/a%20b.txt")
    assert "starts with a dash" in page(client, f"{base}/blob/-dash.txt")


def test_blob_binary_image_and_active_content(web, monkeypatch: pytest.MonkeyPatch) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    html = page(client, f"{base}/blob/bin.dat")
    assert "This is a binary file" in html and 'id="L1"' not in html

    html = page(client, f"{base}/blob/logo.png")
    assert f'<img src="{base}/raw/logo.png" alt="logo.png">' in html
    with monkeypatch.context() as patch:  # an image larger than the text limit
        patch.setattr(browse, "MAX_TEXT_VIEW", 10)
        assert "<img src=" in page(client, f"{base}/blob/logo.png")

    # A text file with an image name is shown as text, not as an image.
    html = page(client, f"{base}/blob/fake.png")
    assert "<img" not in html and "<script>alert" not in html

    # SVG and HTML are shown as source text.
    html = page(client, f"{base}/blob/evil.svg")
    assert "<svg" not in html and "<img" not in html and "onload" in html
    html = page(client, f"{base}/blob/page.html")
    assert "<script>alert('page')" not in html and '<span class="nt">script</span>' in html


def test_blob_markdown_view_and_source_view(web) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    html = page(client, f"{base}/blob/docs/guide.md")
    assert "<h1>Guide</h1>" in html and f'href="{base}/blob/README.md"' in html
    assert f'href="{base}/blob/docs/guide.md?plain=1"' in html
    source = page(client, f"{base}/blob/docs/guide.md?plain=1")
    assert 'id="L1"' in source and "<h1>Guide</h1>" not in source


def test_blob_too_large_shows_facts_only(web, monkeypatch: pytest.MonkeyPatch) -> None:
    client, ids = web
    monkeypatch.setattr(browse, "MAX_TEXT_VIEW", 20)
    html = page(client, f"/repos/{ids['demo']}/blob/README.md")
    assert "too large to show here" in html and "Demo project" not in html
    assert f'href="/repos/{ids["demo"]}/raw/README.md"' in html


# --- raw --------------------------------------------------------------------

RAW_CASES = [
    ("src/app.py", "text/plain; charset=utf-8", "inline"),
    ("evil.svg", "text/plain; charset=utf-8", "inline"),
    ("page.html", "text/plain; charset=utf-8", "inline"),
    ("fake.png", "text/plain; charset=utf-8", "inline"),
    ("README.md", "text/plain; charset=utf-8", "inline"),
    ("logo.png", "image/png", "inline"),
    ("bin.dat", "application/octet-stream", "attachment"),
]


def test_raw_headers(web) -> None:
    client, ids = web
    for name, media, disposition in RAW_CASES:
        resp = client.get(f"/repos/{ids['demo']}/raw/{name}")
        assert resp.status_code == 200, name
        assert resp.headers["content-type"] == media, name
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["content-security-policy"] == "sandbox; default-src 'none'"
        assert resp.headers["content-disposition"].startswith(f"{disposition};"), name
        # Never a type that a browser runs or renders as a document.
        assert not re.match(r"text/html|image/svg|application/xhtml|text/xml|application/xml",
                            resp.headers["content-type"])


def test_raw_content_and_errors(web, monkeypatch: pytest.MonkeyPatch) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    assert client.get(f"{base}/raw/-dash.txt").text == "a file name that starts with a dash\n"
    assert client.get(f"{base}/raw/a%20b.txt").content == b"a file name with a space\n"
    assert client.get(f"{base}/raw/bin.dat").content.startswith(b"\x00\x01\x02")
    assert client.get(f"{base}/raw/feature.txt?rev=feature/x").status_code == 200
    for path in ("raw/feature.txt", "raw/src", "raw/missing", "raw/%2E%2E/x"):
        resp = client.get(f"{base}/{path}")
        assert resp.status_code == 404 and resp.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"/repos/{ids['metaonly']}/raw/README.md").status_code == 404

    monkeypatch.setattr(browse, "MAX_RAW", 10)
    resp = client.get(f"{base}/raw/README.md")
    assert resp.status_code == 413 and "Use git" in resp.text


# --- history ----------------------------------------------------------------

def test_commits_page_and_pagination(web, site: Site,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    html = page(client, f"{base}/commits")
    assert html.index("Add notes") < html.index("Add farewell") < html.index("Initial commit")
    assert "Ann Author" in html and f'href="{base}/commit/{site.shas["main"]}"' in html
    assert 'rel="next"' not in html

    monkeypatch.setattr(browse, "PER_PAGE", 2)
    first = page(client, f"{base}/commits")
    assert "Initial commit" not in first and f'href="{base}/commits?page=2" rel="next"' in first
    second = page(client, f"{base}/commits?page=2")
    assert "Initial commit" in second and "Add notes" not in second
    assert f'href="{base}/commits" rel="prev"' in second and 'rel="next"' not in second
    assert "There are no more commits" in page(client, f"{base}/commits?page=9")

    branch = page(client, f"{base}/commits?rev=feature/x")
    assert "Add the feature file" in branch
    assert f'href="{base}/commits?rev=feature%2Fx&amp;page=2"' in branch


def test_commits_of_one_path(web) -> None:
    client, ids = web
    html = page(client, f"/repos/{ids['demo']}/commits/src/app.py")
    assert "Add farewell" in html and "Initial commit" in html and "Add notes" not in html
    assert "History of src/app.py" in html


def test_commit_page(web, site: Site) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    s = site.shas
    html = page(client, f"{base}/commit/{s['second']}")
    assert "Add farewell" in html and "The body of the message." in html
    assert "Ann Author" in html and "ann@example.com" in html and "Cy Committer" in html
    assert f'href="{base}/commit/{s["first"]}"' in html  # parent
    assert "<strong>2</strong> files changed" in html
    assert "old-name.txt → new-name.txt" in html and "st-renamed" in html
    assert re.search(r'<tr class="add">.*?\+    return &#34;goodbye &#34; \+ name', html)
    assert f'href="{base}/blob/src/app.py?rev={s["second"]}"' in html

    assert "Add farewell" in page(client, f"{base}/commit/{s['second'][:10]}")
    root = page(client, f"{base}/commit/{s['first']}")
    assert "This is a first commit" in root and "Binary file. There is no text diff." in root
    assert "<script>alert('page')" not in root


def test_commit_page_not_found(web) -> None:
    client, ids = web
    for sha in (MISSING_SHA, "--help", "main", "HEAD", "abc", "g" * 40):
        page(client, f"/repos/{ids['demo']}/commit/{sha}", 404)


def test_commit_diff_too_large(web, site: Site,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    client, ids = web
    real = gitread.diff
    monkeypatch.setattr(gitread, "diff", lambda repo, base, head: real(
        repo, base, head, max_files=2, max_bytes=500))
    html = page(client, f"/repos/{ids['demo']}/commit/{site.shas['first']}")
    assert "The diff is too large to show completely." in html
    assert "The list shows the first 2 of 11 files." in html


def test_branches_and_tags_page(web, site: Site) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    html = page(client, f"{base}/branches")
    assert "3 branches" in html and "1 tag" in html
    assert f'href="{base}/code?rev=feature%2Fx"' in html
    assert f'href="{base}/code?rev=--output%3Dx"' in html
    assert '<span class="tag">default</span>' in html
    assert "First release" in html and site.shas["second"][:8] in html
    assert "1 pull request ref (" in html


def test_search(web) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    html = page(client, f"{base}/search?q=needle")
    assert "2 matching lines in 1 file at main" in html
    assert f'href="{base}/blob/notes.txt?plain=1#L3"' in html and "Needle on main" in html

    assert "1 matching line in" in page(client, f"{base}/search?q=needle&case=1")
    branch = page(client, f"{base}/search?q=needle&rev=feature/x")
    assert f'href="{base}/blob/feature.txt?rev=feature%2Fx&amp;plain=1#L1"' in branch
    assert "No line in this revision contains that text" in page(client, f"{base}/search?q=zzzz")
    assert "No line in this revision" in page(client, f"{base}/search?q=--help")
    empty = page(client, f"{base}/search")
    assert "matching" not in empty and 'name="q"' in empty
    escaped = page(client, f"{base}/search?q=%3Cscript%3E")
    assert "<script>" not in escaped.split("</head>")[1].replace(
        '<script src="/static', "")


def test_search_result_cap(web, monkeypatch: pytest.MonkeyPatch) -> None:
    client, ids = web
    monkeypatch.setattr(browse, "MAX_SEARCH_RESULTS", 1)
    html = page(client, f"/repos/{ids['demo']}/search?q=needle")
    assert "There are more matches" in html and "1 matching line in" in html


# --- pull requests and issues ----------------------------------------------

def test_pull_list(web) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    html = page(client, f"{base}/pulls")
    assert "2 pull requests" in html
    assert "Add the feature &lt;b&gt;file&lt;/b&gt;" in html and "Work in progress" in html
    for label, count in (("All", 2), ("Open", 1), ("Closed", 1), ("Merged", 1)):
        assert re.search(rf'{label}\s*<span class="count">{count}</span>', html)
    assert 'style="--label: #d73a4a"' in html and f'href="{base}/pulls?q=bug"' in html
    assert "st-merged" in html and "st-draft" in html

    merged = page(client, f"{base}/pulls?state=merged")
    assert "1 pull request<" in merged and "Work in progress" not in merged
    opened = page(client, f"{base}/pulls?state=open")
    assert "Work in progress" in opened and "Add the feature" not in opened
    assert "Closed includes the merged" in page(client, f"{base}/pulls?state=closed")
    assert "1 pull request that match" in page(client, f"{base}/pulls?q=feature")
    assert "No pull requests match this filter" in page(client, f"{base}/pulls?q=zzz")
    assert "2 pull requests" in page(client, f"{base}/pulls?state=bogus")


def test_issue_list_pagination(web, monkeypatch: pytest.MonkeyPatch) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    monkeypatch.setattr(browse, "PER_PAGE", 2)
    first = page(client, f"{base}/issues")
    assert "6 issues" in first and "Open issue 14" in first and "Open issue 12" not in first
    assert "Page 1 of 3" in first and f'href="{base}/issues?page=2" rel="next"' in first
    last = page(client, f"{base}/issues?page=3")
    assert "Crash on start" in last and 'rel="next"' not in last
    assert f'href="{base}/issues?page=2" rel="prev"' in last
    filtered = page(client, f"{base}/issues?state=open&page=2")
    assert "Page 2 of 3" in filtered
    assert f'href="{base}/issues?state=open&amp;page=3" rel="next"' in filtered


def test_pull_conversation(web) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    html = page(client, f"{base}/pulls/1")
    assert "Add the feature &lt;b&gt;file&lt;/b&gt;" in html and "st-merged" in html
    assert "cy:feature/x" in html and "octo:main" in html
    assert 'href="https://github.com/octo/demo/pull/1" rel="noopener noreferrer nofollow"' in html
    # Body: Markdown, safe, with a local link and task boxes.
    assert f'<a href="{base}/issues/2">#2</a>' in html
    assert "<script>alert('pr')" not in html
    assert '<input type="checkbox" disabled checked> tests' in html
    # One timeline in time order: comment, review comment, approval, merge.
    order = [html.index(text) for text in (
        "A conversation comment.", "Rename this.", "approved these changes", "merged this")]
    assert order == sorted(order)
    assert '<span class="path">feature.txt</span>' in html
    assert re.search(r'<tr class="add"><td class="src">\+needle in the feature branch', html)
    assert "<strong>mo</strong> merged this" in html
    # The empty "COMMENTED" review is not an event of its own.
    assert html.count("<strong>dee</strong>") == 2


def test_pull_commits_tab(web, site: Site) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    html = page(client, f"{base}/pulls/1?tab=commits")
    link = f'<a class="subject" href="{base}/commit/{site.shas["feature"]}">'
    assert f"{link}Add the feature file</a>" in html
    assert "A commit that was force-pushed away" in html and "not in the mirror" in html
    assert f"/commit/{MISSING_SHA}" not in html


def test_pull_files_tab(web, site: Site) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    html = page(client, f"{base}/pulls/1?tab=files")
    # From the merge base, so the later commit on main ("notes.txt") is not in the diff.
    assert "<strong>1</strong> file changed" in html
    assert "feature.txt" in html and "notes.txt" not in html
    assert f'href="{base}/commit/{site.shas["second"]}"' in html
    assert "+needle in the feature branch" in html


def test_pull_files_tab_with_a_missing_head(web) -> None:
    client, ids = web
    html = page(client, f"/repos/{ids['demo']}/pulls/3?tab=files")
    assert "The head commit of this pull request is not in the mirror" in html
    assert "<strong>Pull mirrors</strong>" in html and MISSING_SHA[:10] in html
    assert "No description." in page(client, f"/repos/{ids['demo']}/pulls/3")


def test_issue_page(web, site: Site) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    html = page(client, f"{base}/issues/2")
    assert "Crash on start" in html and "st-closed" in html
    assert 'href="javascript:' not in html
    assert f'href="{base}/commit/{site.shas["first"]}"' in html
    assert f'<a href="{base}/pulls/1">#1</a>' in html and "This was closed" in html

    resp = client.get(f"{base}/issues/1")
    assert (resp.status_code, resp.headers["location"]) == (307, f"{base}/pulls/1")
    for path in ("issues/999", "pulls/999", "pulls/2", "issues/0", "pulls/0"):
        assert "is not in the captured metadata" in page(client, f"{base}/{path}", 404)
    assert client.get(f"{base}/pulls/abc").status_code == 422


def test_releases_page(web) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}"
    html = page(client, f"{base}/releases")
    assert "First release" in html and "<strong>bold</strong>" in html
    assert "app.zip" in html and "2.0 KB" in html
    # The switch is off by default: the page says so and lists names only.
    assert "Release files are not downloaded for this repository." in html
    assert "not downloaded</span>" in html
    assert f'href="{base}/code?rev=v1.0"' in html
    assert 'title="This tag is not in the mirror.">v0.9' in html


# --- honest empty states ----------------------------------------------------

def test_mirror_only(web) -> None:
    client, ids = web
    base = f"/repos/{ids['gitonly']}"
    html = page(client, f"{base}/code")
    assert tab_names(html) == ["Overview", "Code", "Commits"]
    assert "Demo project" in html and "#2</a>" not in html  # no index: no issue link
    for path in ("pulls", "issues", "releases", "pulls/1", "issues/2"):
        text = page(client, f"{base}/{path}")
        assert "The metadata is not captured yet" in text and "Capture metadata" in text
    assert "First release" not in page(client, f"{base}/branches")


def test_metadata_only(web) -> None:
    client, ids = web
    base = f"/repos/{ids['metaonly']}"
    html = page(client, f"{base}/code")
    assert tab_names(html) == ["Overview", "Pull requests", "Issues", "Releases"]
    assert "The mirror is not on disk yet" in html and "Pull mirrors" in html
    for path in ("commits", "branches", "search?q=x", "blob/README.md", f"commit/{'a' * 40}"):
        assert "The mirror is not on disk yet" in page(client, f"{base}/{path}")
    assert "A conversation comment." in page(client, f"{base}/pulls/1")
    assert "is not on disk, so the changed files cannot be computed" in page(
        client, f"{base}/pulls/1?tab=files")
    commits = page(client, f"{base}/pulls/1?tab=commits")
    assert "Add the feature file" in commits and "/commit/" not in commits
    assert "This tag is not in the mirror" in page(client, f"{base}/releases")


def test_no_mirror_and_no_metadata(web) -> None:
    client, ids = web
    base = f"/repos/{ids['nothing']}"
    html = page(client, f"{base}/code")
    assert tab_names(html) == ["Overview"] and "The mirror is not on disk yet" in html
    assert "The metadata is not captured yet" in page(client, f"{base}/pulls")
    assert "This repository has no wiki mirror" in page(client, f"{base}/wiki")


def test_empty_repository(web) -> None:
    client, ids = web
    base = f"/repos/{ids['empty']}"
    for path in ("code", "commits", "search?q=x", "blob/README.md"):
        assert "The repository is empty" in page(client, f"{base}/{path}")
    assert "The mirror has no branches" in page(client, f"{base}/branches")


def test_git_failure_gives_a_message(web, monkeypatch: pytest.MonkeyPatch) -> None:
    client, ids = web

    def fail(*args: object, **kwargs: object) -> None:
        raise gitread.GitError("git log did not finish in 20 seconds.")
    monkeypatch.setattr(gitread, "list_refs", fail)
    html = page(client, f"/repos/{ids['demo']}/code", 500)
    assert "git could not read the mirror" in html and "did not finish in 20 seconds" in html


def test_repo_name_from_the_database_cannot_leave_the_mirror_root(web) -> None:
    client, _ = web
    db.upsert_repo({"full_name": "../../etc", "private": 0, "fork": 0, "archived": 0,
                    "default_branch": "main", "mirror_path": "/etc", "discovered_at": 0})
    bad = next(r["id"] for r in db.list_repos() if r["full_name"] == "../../etc")
    assert client.get(f"/repos/{bad}/code").status_code == 404
    assert client.get(f"/repos/{bad}/pulls").status_code == 404


# --- wiki -------------------------------------------------------------------

def test_wiki_uses_the_same_pages(web) -> None:
    client, ids = web
    base = f"/repos/{ids['demo']}/wiki"
    html = page(client, base)
    assert f'href="{base}/blob/Home.md"' in html and f'href="{base}/blob/Setup.md"' in html
    assert "<h1>Wiki home</h1>" in html
    assert 'class="active" aria-current="page">Wiki</a>' in html
    assert "Search in the code" not in html

    assert "<h1>Setup</h1>" in page(client, f"{base}/blob/Setup.md")
    assert client.get(f"{base}/raw/Setup.md").text == "# Setup\n"
    history = page(client, f"{base}/commits")
    assert "Write the wiki" in history
    sha = re.search(rf'href="{base}/commit/([0-9a-f]{{40}})"', history).group(1)
    assert "Home.md" in page(client, f"{base}/commit/{sha}")
    # The code mirror and the wiki mirror do not mix.
    page(client, f"{base}/blob/README.md", 404)
    page(client, f"/repos/{ids['demo']}/blob/Home.md", 404)
