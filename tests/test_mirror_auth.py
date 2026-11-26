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


@pytest.fixture
def recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Run mirror_repo with git replaced by a recorder that always succeeds."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "0\t.", "")

    monkeypatch.setattr(mirror, "_run", fake_run)
    monkeypatch.setattr(runtime, "mirror_root", lambda: tmp_path / "mirrors")
    return calls


def test_mirror_commands_without_token(recorded: list[list[str]],
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "github_token", lambda: "")
    ok, _msg, _size = mirror.mirror_repo(
        "octo/demo", fetch_lfs=True, include_wiki=True, has_wiki=True)
    assert ok
    git_calls = [c for c in recorded if c[0] == "git"]
    assert len(git_calls) == 3  # clone, lfs, wiki clone
    for cmd in git_calls:
        assert "-c" not in cmd
        assert not any("extraHeader" in part or "Authorization" in part for part in cmd)
    assert git_calls[0][1:4] == ["clone", "--mirror", "https://github.com/octo/demo.git"]
    assert "https://github.com/octo/demo.wiki.git" in git_calls[2]


def test_mirror_commands_with_token(recorded: list[list[str]],
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "github_token", lambda: "tok123")
    mirror.mirror_repo("octo/demo", fetch_lfs=True, include_wiki=True, has_wiki=True)
    git_calls = [c for c in recorded if c[0] == "git"]
    assert len(git_calls) == 3
    for cmd in git_calls:
        assert cmd[1] == "-c"
        assert cmd[2].startswith("http.extraHeader=Authorization: Basic ")


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
    # `du` is a Linux tool; the size is not the subject of this test.
    monkeypatch.setattr(mirror, "_dir_size", lambda path: 1)

    dest = tmp_path / "mirrors" / "octo" / "demo.git"
    ok, msg, _size = mirror.mirror_repo(
        "octo/demo", fetch_lfs=False, include_wiki=False, has_wiki=False)
    assert ok, msg
    assert msg == "clone"
    assert (dest / "HEAD").exists()

    ok, msg, _size = mirror.mirror_repo(
        "octo/demo", fetch_lfs=False, include_wiki=False, has_wiki=False)
    assert ok, msg
    assert msg == "update"
