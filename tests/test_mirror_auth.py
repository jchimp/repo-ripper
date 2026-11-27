"""The git commands of the mirror step, with and without a token."""
import os
import subprocess
from pathlib import Path

import pytest

from app import mirror, runtime

_TOKEN = "ghp_SECRETsecretSECRETsecret1234"
_KEY = "http.https://github.com/.extraHeader"


def test_no_token_gives_no_auth_header() -> None:
    assert mirror._auth_env("") == {}


def test_token_goes_in_the_environment_for_the_github_host_only(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    env = mirror._auth_env("tok123")
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == _KEY
    assert env["GIT_CONFIG_VALUE_0"] == mirror._auth_header("tok123")
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert len(env) == 3


def test_no_auth_header_when_the_base_is_not_a_web_url(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mirror, "GIT_BASE_URL", tmp_path.as_posix())
    assert mirror._auth_env("tok123") == {}
    monkeypatch.setattr(mirror, "GIT_BASE_URL", "ssh://git@github.com")
    assert mirror._auth_env("tok123") == {}


def test_config_values_of_the_environment_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    env = mirror._auth_env("tok123")
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert env["GIT_CONFIG_KEY_2"] == _KEY
    assert "GIT_CONFIG_KEY_0" not in env


def test_git_reads_the_header_for_github_and_for_no_other_host(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Real git: the value in the environment applies to github.com URLs only."""
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    env = {**os.environ, **mirror._auth_env(_TOKEN)}

    def header_for(url: str) -> str:
        res = subprocess.run(["git", "config", "--get-urlmatch", "http.extraHeader", url],
                             env=env, capture_output=True, text=True, timeout=60)
        return res.stdout.strip()

    assert header_for("https://github.com/octo/demo.git") == mirror._auth_header(_TOKEN)
    assert header_for("https://github.com/octo/demo.git/info/lfs/objects/batch")
    assert header_for("https://objects.githubusercontent.com/some/object") == ""
    assert header_for("https://github.com.example.net/octo/demo.git") == ""
    assert header_for("http://github.com/octo/demo.git") == ""


def test_token_is_not_in_the_arguments_of_any_command(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the commands that talk to GitHub get the token, and only in the environment."""
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    monkeypatch.setattr(runtime, "github_token", lambda: _TOKEN)
    monkeypatch.setattr(runtime, "mirror_root", lambda: tmp_path)
    for name in ("demo.git", "demo.wiki.git"):                # these get an update
        (tmp_path / "octo" / name).mkdir(parents=True)
        (tmp_path / "octo" / name / "HEAD").write_text("ref: refs/heads/main\n")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append((cmd, kwargs["env"]))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mirror.subprocess, "run", fake_run)
    mirror.mirror_repo("octo/demo", fetch_lfs=True, include_wiki=True, has_wiki=True)
    mirror.mirror_repo("octo/fresh", fetch_lfs=False, include_wiki=False, has_wiki=False)

    encoded = mirror._basic_credential(_TOKEN)
    with_token: list[str] = []
    for cmd, env in calls:
        assert cmd[0] == "git"
        assert not any(_TOKEN in arg or encoded in arg or "extraHeader" in arg for arg in cmd)
        remote = "clone" in cmd or "update" in cmd or "lfs" in cmd
        if remote:
            assert env["GIT_CONFIG_KEY_0"] == _KEY
            assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {encoded}"
            with_token.append(next(a for a in cmd if a in ("clone", "update", "lfs")))
        else:
            assert "GIT_CONFIG_KEY_0" not in env and "GIT_CONFIG_COUNT" not in env
            assert encoded not in "".join(env.values())
    # Mirror update, LFS fetch, wiki update, and the clone of the second repository.
    assert with_token == ["update", "lfs", "update", "clone"]


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
