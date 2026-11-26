"""The repository name / URL parser."""
import pytest

from app.watch import parse_repo_ref

GOOD = [
    ("octocat/Hello-World", ("octocat", "Hello-World")),
    ("  octocat/Hello-World  ", ("octocat", "Hello-World")),
    ("octocat/Hello-World.git", ("octocat", "Hello-World")),
    ("a/b", ("a", "b")),
    ("my-org/my.repo_name-2", ("my-org", "my.repo_name-2")),
    ("octocat/.github", ("octocat", ".github")),
    ("https://github.com/octocat/Hello-World", ("octocat", "Hello-World")),
    ("https://github.com/octocat/Hello-World.git", ("octocat", "Hello-World")),
    ("https://github.com/octocat/Hello-World/", ("octocat", "Hello-World")),
    ("https://github.com/octocat/Hello-World.git/", ("octocat", "Hello-World")),
    ("https://github.com/octocat/Hello-World/tree/main", ("octocat", "Hello-World")),
    ("https://github.com/octocat/Hello-World/tree/main/src/a.py", ("octocat", "Hello-World")),
    ("https://github.com/octocat/Hello-World/pull/12", ("octocat", "Hello-World")),
    ("https://github.com/octocat/Hello-World/pull/12#issuecomment-1", ("octocat", "Hello-World")),
    ("https://github.com/octocat/Hello-World?tab=readme-ov-file", ("octocat", "Hello-World")),
    ("https://github.com/octocat/Hello-World#readme", ("octocat", "Hello-World")),
    ("HTTPS://GitHub.com/Octocat/Hello-World", ("Octocat", "Hello-World")),
    ("git@github.com:octocat/Hello-World.git", ("octocat", "Hello-World")),
    ("git@github.com:octocat/Hello-World", ("octocat", "Hello-World")),
    ("git@github.com:my-org/my.repo.git", ("my-org", "my.repo")),
]

BAD = [
    "",
    "   ",
    "octocat",
    "octocat/",
    "/Hello-World",
    "octocat/Hello-World/",
    "octocat/Hello-World/tree/main",
    "octocat/Hello World",
    "octo cat/Hello-World",
    "octocat\\Hello-World",
    "-octocat/Hello-World",
    "octo_cat/Hello-World",
    "octocat/Hello-World!",
    "octocat/..",
    "octocat/.",
    "octocat/.git",
    "../etc/passwd",
    "octocat/../../etc",
    "a" * 40 + "/repo",
    "octocat/" + "r" * 101,
    "http://github.com/octocat/Hello-World",
    "https://github.com",
    "https://github.com/",
    "https://github.com/octocat",
    "https://github.com/octocat/",
    "https://github.com//Hello-World",
    "https://gitlab.com/octocat/Hello-World",
    "https://github.com.evil.example/octocat/Hello-World",
    "https://evil.example/github.com/octocat/Hello-World",
    "https://user:secret@github.com/octocat/Hello-World",
    "https://github.com:8443/octocat/Hello-World",
    "https://api.github.com/repos/octocat/Hello-World",
    "https://github.com/octocat/Hello-World /tree/main",
    "https://github.com/octocat/..",
    "github.com/octocat/Hello-World",
    "ssh://git@github.com/octocat/Hello-World.git",
    "git@gitlab.com:octocat/Hello-World.git",
    "git@github.com:octocat",
    "git@github.com/octocat/Hello-World.git",
    "octocat/Hello-World\nother/repo",
    "octocat/Hello-World; rm -rf /",
    "--upload-pack=x/y",
]


@pytest.mark.parametrize(("text", "expected"), GOOD)
def test_accepted_forms(text: str, expected: tuple[str, str]) -> None:
    assert parse_repo_ref(text) == expected


@pytest.mark.parametrize("text", BAD)
def test_refused_forms(text: str) -> None:
    with pytest.raises(ValueError, match="This is not a repository name"):
        parse_repo_ref(text)
