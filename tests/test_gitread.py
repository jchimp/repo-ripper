"""The git reader, on local repositories made in a temp folder."""
import os
import sys
from pathlib import Path

import pytest

from app import gitread

from .browse_fixture import MISSING_SHA, Site, build_site


@pytest.fixture(scope="session")
def site(tmp_path_factory: pytest.TempPathFactory) -> Site:
    return build_site(tmp_path_factory.mktemp("browse"))

HOSTILE_REVS = [
    "", " ", "-h", "--help", "--output=/tmp/x", "--upload-pack=touch x", "--git-dir=/",
    "main..feature/x", "main...feature/x", "main^{tree}", "main^", "HEAD~1", "main:README.md",
    ":/Initial", "main@{1}", "@{-1}", "a b", "main\n--help", "main\x00", "*", "feature/*",
    "refs/heads/[m]ain", "main.lock", "feature/", "/main", "x" * 300, "no-such-branch",
]


def test_repo_exists(site: Site, tmp_path: Path) -> None:
    assert gitread.repo_exists(site.demo)
    assert not gitread.repo_exists(tmp_path)
    assert not gitread.repo_exists(tmp_path / "missing.git")


def test_git_environment_is_minimal(site: Site) -> None:
    env = gitread._env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert "HOME" not in env and "GIT_DIR" not in env
    cmd = gitread._command(site.demo, ["status"])
    safe = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-c" and "safe.directory" in cmd[i + 1]]
    assert safe and all(v != "safe.directory=*" for v in safe)
    assert f"safe.directory={site.demo.as_posix()}" in safe
    assert "--literal-pathspecs" in cmd


def test_list_refs(site: Site) -> None:
    refs = gitread.list_refs(site.demo)
    assert refs.default_branch == "main"
    assert {b.name for b in refs.branches} == {"main", "feature/x", "--output=x"}
    assert refs.branches[0].name == "main"  # newest first
    assert [t.name for t in refs.tags] == ["v1.0"]
    # An annotated tag is peeled to its commit.
    assert refs.tags[0].sha == site.shas["second"]
    assert refs.pull_count is None and refs.pulls == ()
    main = next(b for b in refs.branches if b.name == "main")
    assert (main.sha, main.subject, main.kind) == (site.shas["main"], "Add notes", "branch")


def test_list_refs_counts_and_lists_pull_refs(site: Site) -> None:
    assert gitread.list_refs(site.demo, count_pulls=True).pull_count == 1
    refs = gitread.list_refs(site.demo, list_pulls=True)
    assert [(p.name, p.sha) for p in refs.pulls] == [("pull/1/head", site.shas["feature"])]


def test_list_refs_of_an_empty_repository(site: Site) -> None:
    refs = gitread.list_refs(site.root / "octo" / "empty.git")
    assert refs.default_branch is None and refs.branches == () and refs.tags == ()


def test_resolve(site: Site) -> None:
    s = site.shas
    assert gitread.resolve(site.demo, "main") == s["main"]
    assert gitread.resolve(site.demo, "feature/x") == s["feature"]
    assert gitread.resolve(site.demo, "v1.0") == s["second"]
    assert gitread.resolve(site.demo, "HEAD") == s["main"]
    assert gitread.resolve(site.demo, s["first"]) == s["first"]
    assert gitread.resolve(site.demo, s["first"][:10]) == s["first"]
    assert gitread.resolve(site.demo, "refs/pull/1/head") == s["feature"]
    assert gitread.resolve(site.demo, MISSING_SHA) is None


def test_a_ref_named_like_an_option_is_a_ref_not_an_option(site: Site) -> None:
    assert gitread.resolve(site.demo, "--output=x") == site.shas["second"]


@pytest.mark.parametrize("rev", HOSTILE_REVS)
def test_hostile_revisions_resolve_to_nothing(site: Site, rev: str, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    before = sorted(p.name for p in site.demo.iterdir())
    assert gitread.resolve(site.demo, rev) is None
    # No option ran: nothing new in the working folder or in the mirror.
    assert list(tmp_path.iterdir()) == []
    assert sorted(p.name for p in site.demo.iterdir()) == before


def test_functions_refuse_a_value_that_is_not_a_full_sha(site: Site) -> None:
    for bad in ("main", "--help", "HEAD", site.shas["main"][:10], site.shas["main"].upper()):
        with pytest.raises(ValueError):
            gitread.list_tree(site.demo, bad, "")
        with pytest.raises(ValueError):
            gitread.log(site.demo, bad)
        with pytest.raises(ValueError):
            gitread.diff(site.demo, None, bad)
        with pytest.raises(ValueError):
            gitread.grep(site.demo, bad, "x")


def test_clean_path() -> None:
    assert gitread.clean_path("") == ""
    assert gitread.clean_path("/src//app.py/") == "src/app.py"
    assert gitread.clean_path("-dash.txt") == "-dash.txt"
    for bad in ("..", "a/../b", "./a", "a/./b", "a\x00b", "x" * 5000, None, 3):
        assert gitread.clean_path(bad) is None


def test_list_tree(site: Site) -> None:
    tree = gitread.list_tree(site.demo, site.shas["main"], "")
    names = [e.name for e in tree.entries]
    assert names[:2] == ["docs", "src"]  # folders first
    assert {"README.md", "a b.txt", "-dash.txt", "bin.dat", "new-name.txt"} <= set(names)
    assert "old-name.txt" not in names and not tree.truncated
    readme = next(e for e in tree.entries if e.name == "README.md")
    assert (readme.kind, readme.path, readme.mode) == ("blob", "README.md", "100644")
    assert readme.size and readme.size > 50

    sub = gitread.list_tree(site.demo, site.shas["main"], "src")
    assert [(e.name, e.path) for e in sub.entries] == [("app.py", "src/app.py")]
    assert gitread.list_tree(site.demo, site.shas["main"], "README.md") is None
    assert gitread.list_tree(site.demo, site.shas["main"], "missing") is None


@pytest.mark.parametrize("path", ["../..", "src/../../x", "a\x00b"])
def test_hostile_paths_are_refused(site: Site, path: str) -> None:
    with pytest.raises(ValueError):
        gitread.list_tree(site.demo, site.shas["main"], path)
    with pytest.raises(ValueError):
        gitread.read_blob(site.demo, site.shas["main"], path, 100)


def test_path_info_takes_the_path_as_plain_text(site: Site) -> None:
    sha = site.shas["main"]
    assert gitread.path_info(site.demo, sha, "a b.txt").kind == "blob"
    assert gitread.path_info(site.demo, sha, "-dash.txt").kind == "blob"
    assert gitread.path_info(site.demo, sha, "src").kind == "tree"
    # No glob, no pathspec magic, no option.
    for path in ("*.txt", ":(glob)**", ":/", "--help", "-z", "src/*"):
        assert gitread.path_info(site.demo, sha, path) is None


def test_read_blob(site: Site) -> None:
    sha = site.shas["main"]
    blob = gitread.read_blob(site.demo, sha, "-dash.txt", 1000)
    assert blob.text() == "a file name that starts with a dash\n"
    assert (blob.binary, blob.truncated, blob.size) == (False, False, len(blob.data))
    assert gitread.read_blob(site.demo, sha, "a b.txt", 1000).text().startswith("a file name")

    binary = gitread.read_blob(site.demo, sha, "bin.dat", 1000)
    assert binary.binary and binary.data.startswith(b"\x00\x01\x02")

    cut = gitread.read_blob(site.demo, sha, "README.md", 10)
    assert cut.truncated and len(cut.data) == 10 and cut.size > 10

    assert gitread.read_blob(site.demo, sha, "src", 1000) is None
    assert gitread.read_blob(site.demo, sha, "missing.txt", 1000) is None


def test_readme(site: Site) -> None:
    blob = gitread.readme(site.demo, site.shas["main"])
    assert blob.path == "README.md" and "# Demo project" in blob.text()
    assert gitread.readme(site.demo, site.shas["main"], "src") is None


def test_log(site: Site) -> None:
    s = site.shas
    commits = gitread.log(site.demo, s["main"])
    assert [c.subject for c in commits] == ["Add notes", "Add farewell", "Initial commit"]
    assert [c.sha for c in commits] == [s["main"], s["second"], s["first"]]
    top = commits[0]
    assert (top.author_name, top.author_email) == ("Ann Author", "ann@example.com")
    assert top.committer_name == "Cy Committer" and top.parents == (s["second"],)
    assert commits[-1].parents == ()
    assert top.author_time > commits[1].author_time

    assert [c.sha for c in gitread.log(site.demo, s["main"], skip=1, limit=1)] == [s["second"]]
    assert gitread.log(site.demo, s["main"], skip=10) == ()
    by_path = gitread.log(site.demo, s["main"], "src/app.py")
    assert [c.subject for c in by_path] == ["Add farewell", "Initial commit"]
    assert [c.subject for c in gitread.log(site.demo, s["main"], "-dash.txt")] == \
        ["Initial commit"]
    assert gitread.log(site.demo, s["main"], "missing.txt") == ()


def test_commit(site: Site) -> None:
    info = gitread.commit(site.demo, site.shas["second"])
    assert info.subject == "Add farewell"
    assert info.body == "The body of the message.\nSecond line."
    assert info.parents == (site.shas["first"],)
    assert gitread.commit(site.demo, MISSING_SHA) is None


def test_diff_of_a_commit(site: Site) -> None:
    s = site.shas
    result = gitread.diff(site.demo, s["first"], s["second"])
    assert not result.too_large and result.total_files == 2
    by_path = {f.path: f for f in result.files}
    renamed = by_path["new-name.txt"]
    assert (renamed.status, renamed.old_path, renamed.hunks) == ("renamed", "old-name.txt", ())
    app = by_path["src/app.py"]
    assert (app.status, app.added, app.deleted, app.binary) == ("modified", 4, 0, False)
    added = [ln for h in app.hunks for ln in h.lines if ln.kind == "+"]
    assert [ln.text for ln in added][-1] == '    return "goodbye " + name'
    assert added[-1].new_no == 7 and added[-1].old_no is None
    context = [ln for h in app.hunks for ln in h.lines if ln.kind == " "]
    assert context[0].old_no == context[0].new_no == 1
    assert (result.added, result.deleted) == (4, 0)


def test_diff_of_a_first_commit_and_binary_files(site: Site) -> None:
    result = gitread.diff(site.demo, None, site.shas["first"])
    by_path = {f.path: f for f in result.files}
    assert by_path["README.md"].status == "added"
    assert by_path["a b.txt"].hunks[0].lines[0].text == "a file name with a space"
    assert by_path["-dash.txt"].status == "added"
    assert by_path["bin.dat"].binary and by_path["bin.dat"].added is None
    assert by_path["bin.dat"].hunks == ()


def test_diff_too_large(site: Site) -> None:
    s = site.shas
    small = gitread.diff(site.demo, None, s["first"], max_bytes=600)
    assert small.too_large and small.total_files == 11
    assert len(small.files) == 11  # the file list stays complete
    assert any(f.omitted for f in small.files)
    few = gitread.diff(site.demo, None, s["first"], max_files=3)
    assert few.too_large and len(few.files) == 3 and few.total_files == 11


def test_diff_with_a_missing_commit_is_an_error(site: Site) -> None:
    with pytest.raises(gitread.GitError):
        gitread.diff(site.demo, MISSING_SHA, site.shas["main"])


def test_merge_base(site: Site) -> None:
    s = site.shas
    assert gitread.merge_base(site.demo, s["main"], s["feature"]) == s["second"]
    assert gitread.merge_base(site.demo, s["main"], MISSING_SHA) is None


def test_commits_exist(site: Site) -> None:
    s = site.shas
    found = gitread.commits_exist(site.demo, [s["main"], MISSING_SHA, "--help", s["first"]])
    assert found == {s["main"], s["first"]}
    assert gitread.commits_exist(site.demo, []) == set()


def test_grep(site: Site) -> None:
    sha = site.shas["main"]
    result = gitread.grep(site.demo, sha, "needle")
    assert [(m.path, m.line_no) for m in result.matches] == [("notes.txt", 1), ("notes.txt", 3)]
    assert result.matches[0].text == "Needle on main" and not result.truncated

    exact = gitread.grep(site.demo, sha, "needle", ignore_case=False)
    assert [m.line_no for m in exact.matches] == [3]
    # A fixed string: "." is a dot, not "any character".
    assert [m.line_no for m in gitread.grep(site.demo, sha, "needle.again").matches] == [3]
    assert gitread.grep(site.demo, sha, "needle.on").matches == ()
    assert gitread.grep(site.demo, sha, "").matches == ()
    assert gitread.grep(site.demo, sha, "binary").matches == ()  # binary files are skipped

    limited = gitread.grep(site.demo, sha, "needle", limit=1)
    assert len(limited.matches) == 1 and limited.truncated
    other = gitread.grep(site.demo, site.shas["feature"], "needle")
    assert [m.path for m in other.matches] == ["feature.txt"]


@pytest.mark.parametrize("pattern", ["-e", "--help", "--open-files-in-pager=x", "-O", "-f/etc"])
def test_grep_pattern_that_looks_like_an_option(site: Site, pattern: str, tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert gitread.grep(site.demo, site.shas["main"], pattern).matches == ()
    assert list(tmp_path.iterdir()) == []


def test_git_call_timeout(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitread, "_command", lambda repo, args: [
        sys.executable, "-c", "import time; time.sleep(30)"])
    with pytest.raises(gitread.GitError, match="did not finish"):
        gitread._run(site.demo, ["log"], timeout=0.5)


def test_git_output_cap(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitread, "_command", lambda repo, args: [
        sys.executable, "-c", "import sys; sys.stdout.write('x' * 500000)"])
    code, out, truncated = gitread._run(site.demo, ["log"], max_bytes=1000)
    assert truncated and len(out) == 1000


def test_git_not_installed(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitread, "_command", lambda repo, args: ["no-such-git-program-xyz"])
    with pytest.raises(gitread.GitError, match="not found"):
        gitread.resolve(site.demo, "main")


def test_the_mirror_is_not_changed_by_reads(site: Site) -> None:
    def snapshot() -> list[tuple[str, int]]:
        return sorted((p.relative_to(site.demo).as_posix(), p.stat().st_mtime_ns)
                      for p in site.demo.rglob("*") if p.is_file())
    before = snapshot()
    sha = gitread.resolve(site.demo, "main")
    gitread.clear_cache()
    gitread.list_refs(site.demo, list_pulls=True)
    gitread.list_tree(site.demo, sha, "")
    gitread.log(site.demo, sha)
    gitread.diff(site.demo, None, sha)
    gitread.grep(site.demo, sha, "needle")
    assert snapshot() == before
