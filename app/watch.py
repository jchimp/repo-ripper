"""Watch public repositories that the token's account does not own.

Three operations, used by the routes and by the scan job:

- `add_repo`: add one repository by name or URL.
- `add_owner`: watch all public repositories of one user or organisation.
- `discover_owner`: list one watched owner and add its new repositories.

Each one returns or raises a message in plain text for the user.
"""
import re
import time
from dataclasses import dataclass
from sqlite3 import Row

from . import db, github

# GitHub rules: a login has letters, digits and hyphens, 39 characters at most.
# A repository name has letters, digits, '.', '_' and '-'.
_OWNER = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"
_NAME = r"[A-Za-z0-9._-]{1,100}"

_OWNER_RE = re.compile(_OWNER)
_SHORT_RE = re.compile(rf"(?P<owner>{_OWNER})/(?P<name>{_NAME})")
# After owner/name a URL can have a trailing slash, more path (/tree/main,
# /pull/12), a query string and a fragment. No white space anywhere.
_HTTPS_RE = re.compile(
    rf"(?i:https://github\.com)/(?P<owner>{_OWNER})/(?P<name>{_NAME})"
    r"(?:/[^\s?#]*)?(?:[?#]\S*)?"
)
_SSH_RE = re.compile(rf"(?i:git@github\.com):(?P<owner>{_OWNER})/(?P<name>{_NAME})/?")

BAD_REF_MESSAGE = (
    "This is not a repository name. Use owner/name, "
    "https://github.com/owner/name or git@github.com:owner/name.git."
)
BAD_LOGIN_MESSAGE = (
    "This is not a GitHub account name. Use the name of a user or an "
    "organisation, for example octocat."
)


def parse_repo_ref(text: str) -> tuple[str, str]:
    """Read `owner/name` from a repository name or a GitHub URL.

    Accepted forms: `owner/name`, `https://github.com/owner/name` (also with
    `.git`, a trailing slash or more path) and `git@github.com:owner/name.git`.

    Returns:
        `(owner, name)` as typed. The caller gets the canonical spelling from
        the GitHub API.

    Raises:
        ValueError: The text has none of the accepted forms.
    """
    value = text.strip()
    for pattern in (_SHORT_RE, _HTTPS_RE, _SSH_RE):
        match = pattern.fullmatch(value)
        if match:
            break
    else:
        raise ValueError(BAD_REF_MESSAGE)
    owner, name = match["owner"], match["name"]
    name = name.removesuffix(".git")
    # '.' and '..' are path parts, not names: the mirror folder is built from this.
    if name in ("", ".", ".."):
        raise ValueError(BAD_REF_MESSAGE)
    return owner, name


def safe_full_name(full_name: str) -> bool:
    """True when a `full_name` from the API is safe to use as a folder path."""
    match = _SHORT_RE.fullmatch(full_name)
    return bool(match) and match["name"] not in (".", "..")


def _repo_row(info: dict, now: float) -> dict:
    return {
        "full_name": info["full_name"],
        "private": info["private"],
        "fork": info["fork"],
        "archived": info["archived"],
        "default_branch": info["default_branch"],
        "mirror_path": f"{info['full_name']}.git",
        "discovered_at": now,
        "source": "watched",
    }


@dataclass(frozen=True)
class Result:
    """Outcome of an add operation. `name` is the canonical name on success."""
    ok: bool
    message: str
    name: str | None = None


def _already(full_name: str) -> Result:
    return Result(False, f"{full_name} is already in the list. Nothing changed.")


def add_repo(text: str) -> Result:
    """Add one repository as `source='watched'`, after a check with GitHub."""
    try:
        owner, name = parse_repo_ref(text)
    except ValueError as exc:
        return Result(False, str(exc))

    # Before the API call: an unauthenticated address has 60 calls per hour.
    existing = db.get_repo_by_name(f"{owner}/{name}")
    if existing:
        return _already(existing["full_name"])

    try:
        info = github.get_repo(owner, name)
    except github.NotFound:
        return Result(False, (
            f"The repository {owner}/{name} does not exist, or it is private "
            "and not visible with the current token."
        ))
    except github.GitHubError as exc:
        return Result(False, str(exc))

    full_name = info["full_name"]
    if not safe_full_name(full_name):
        return Result(False, "GitHub sent a repository name that is not valid.")
    # Again with the canonical name: the typed name can be an old name.
    existing = db.get_repo_by_name(full_name)
    if existing:
        return _already(existing["full_name"])

    db.upsert_repo(_repo_row(info, time.time()))
    return Result(True, f"Added {full_name}. The next pull job makes the mirror.", full_name)


def add_owner(text: str, include_forks: bool, include_archived: bool) -> Result:
    """Watch all public repositories of one user or organisation."""
    login = text.strip().removeprefix("@")
    if not _OWNER_RE.fullmatch(login):
        return Result(False, BAD_LOGIN_MESSAGE)

    existing = db.get_watch_owner_by_login(login)
    if existing:
        return Result(False, f"{existing['login']} is already a watched owner. Nothing changed.")

    try:
        owner = github.get_owner(login)
    except github.NotFound:
        return Result(False, f"GitHub has no user or organisation with the name {login}.")
    except github.GitHubError as exc:
        return Result(False, str(exc))

    canonical = owner["login"]
    if not _OWNER_RE.fullmatch(canonical):
        return Result(False, "GitHub sent an account name that is not valid.")
    if not db.add_watch_owner(canonical, owner["kind"], include_forks, include_archived):
        return Result(False, f"{canonical} is already a watched owner. Nothing changed.")
    return Result(
        True,
        f"Added {canonical}. The next scan job adds the public repositories of {canonical}.",
        canonical,
    )


def discover_owner(owner: Row) -> tuple[int, int]:
    """List one watched owner on GitHub and add its new repositories.

    Args:
        owner: A row of the `watch_owners` table.

    Returns:
        `(found, added)`: repositories that agree with the owner's fork and
        archived options, and how many of them were new rows.

    Raises:
        github.GitHubError: The API call failed. The message is for the user.
    """
    try:
        repos = github.list_owner_repos(owner["login"], owner["kind"])
    except github.NotFound as exc:
        raise github.GitHubError(
            f"GitHub has no {'organisation' if owner['kind'] == 'org' else 'user'} "
            f"with the name {owner['login']}."
        ) from exc

    now = time.time()
    found = added = 0
    for info in repos:
        if info["fork"] and not owner["include_forks"]:
            continue
        if info["archived"] and not owner["include_archived"]:
            continue
        if not safe_full_name(info["full_name"]):
            continue
        found += 1
        if db.upsert_repo(_repo_row(info, now)):
            added += 1
    return found, added
