"""The git commands of the mirror step, with and without a token."""
import subprocess
from pathlib import Path

import pytest

from app import mirror, runtime


def test_git_base_without_token_has_no_auth_header() -> None:
    assert mirror._git_base("") == ["git"]


def test_git_base_with_token_sends_the_header() -> None:
    cmd = mirror._git_base("tok123")
    assert cmd[:2] == ["git", "-c"]
    assert cmd[2].startswith("http.extraHeader=Authorization: Basic ")
    assert len(cmd) == 3


def test_git_prefix_follows_the_configured_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "github_token", lambda: "")
    assert mirror._git() == ["git"]
    monkeypatch.setattr(runtime, "github_token", lambda: "tok123")
    assert mirror._git()[1] == "-c"


def test_clone_urls() -> None:
    assert mirror.clone_url("octo/demo") == "https://github.com/octo/demo.git"
    assert mirror.clone_url("octo/demo.wiki") == "https://github.com/octo/demo.wiki.git"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def test_real_clone_and_update_without_token(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """A public clone needs no credential: clone a local bare repository."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", ".", cwd=work)
    (work / "README.md").write_text("demo\n", encoding="utf-8")
    _git("add", "README.md", cwd=work)
    _git("commit", "-q", "-m", "First.", cwd=work)
    remote = tmp_path / "remote"
    (remote / "octo").mkdir(parents=True)
    _git("clone", "-q", "--bare", str(work), str(remote / "octo" / "demo.git"), cwd=tmp_path)

    monkeypatch.setattr(mirror, "GIT_BASE_URL", remote.as_posix())
    monkeypatch.setattr(runtime, "github_token", lambda: "")
    monkeypatch.setattr(runtime, "mirror_root", lambda: tmp_path / "mirrors")

    dest = tmp_path / "mirrors" / "octo" / "demo.git"
    first = mirror.mirror_repo(
        "octo/demo", fetch_lfs=False, include_wiki=False, has_wiki=False)
    assert first.ok, first.message
    assert first.action == "clone"
    assert (dest / "HEAD").exists()

    second = mirror.mirror_repo(
        "octo/demo", fetch_lfs=False, include_wiki=False, has_wiki=False)
    assert second.ok, second.message
    assert second.action == "update"
