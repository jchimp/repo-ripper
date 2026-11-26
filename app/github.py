"""GitHub REST API access: discovery and token check.

Two kinds of discovery:

- `list_all_repos` uses the token and sees everything the token is scoped
  for: personal repos (public + private), collaborator repos, and org repos
  where the user is a member.
- `get_repo`, `get_owner` and `list_owner_repos` read public data. They work
  with or without a token. Without a token GitHub allows 60 requests per hour
  per IP address, so a rate-limit answer becomes a `RateLimited` error with a
  message the user can act on.

Every error raised here is a `GitHubError` whose text is safe to show in the
UI and to write to a job log: it never contains the token.
"""
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from . import runtime
from .config import get_settings

# Tests set this to an `httpx.MockTransport`. None means the real network.
_transport: httpx.BaseTransport | None = None

PER_PAGE = 100

MSG_REJECTED = "GitHub rejected the token. The token is wrong or expired."
MSG_NETWORK = (
    "The app cannot reach api.github.com from the container. "
    "Check the network connection and the DNS of the Docker host."
)


class GitHubError(RuntimeError):
    """A GitHub API problem. The message is plain text for the user."""


class NotFound(GitHubError):
    """HTTP 404: the thing does not exist, or the token cannot see it."""


class NetworkError(GitHubError):
    """The API could not be reached at all."""


class TokenRejected(GitHubError):
    """HTTP 401: the token is wrong or expired."""


class RateLimited(GitHubError):
    """The request quota is used up. `reset_at` is a Unix time, when known."""

    def __init__(self, message: str, reset_at: float | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


def _headers(token: str) -> dict[str, str]:
    """Request headers. No `Authorization` header when the token is empty."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _client(token: str | None = None) -> httpx.Client:
    """Make a client. `token` None = the configured token; "" = no token."""
    # follow_redirects: GitHub answers 301 for a renamed repository or owner.
    return httpx.Client(
        timeout=30,
        base_url=get_settings().github_api,
        headers=_headers(runtime.github_token() if token is None else token),
        follow_redirects=True,
        transport=_transport,
    )


def _api_message(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return ""
    return str(body.get("message") or "") if isinstance(body, dict) else ""


def _rate_limit_error(r: httpx.Response, authed: bool) -> RateLimited | None:
    """Return a RateLimited error when the answer is a rate-limit refusal."""
    if r.status_code not in (403, 429):
        return None
    limited = (
        r.status_code == 429
        or r.headers.get("X-RateLimit-Remaining") == "0"
        or "Retry-After" in r.headers
        or "rate limit" in _api_message(r).lower()
    )
    if not limited:
        return None

    reset_at: float | None = None
    try:
        if r.headers.get("X-RateLimit-Remaining") == "0" and "X-RateLimit-Reset" in r.headers:
            reset_at = float(r.headers["X-RateLimit-Reset"])
        elif "Retry-After" in r.headers:
            reset_at = time.time() + float(r.headers["Retry-After"])
        elif "X-RateLimit-Reset" in r.headers:
            reset_at = float(r.headers["X-RateLimit-Reset"])
    except ValueError:
        reset_at = None

    msg = "GitHub rate limit reached."
    if reset_at is not None:
        msg += f" It resets at {time.strftime('%H:%M', time.localtime(reset_at))}."
    else:
        msg += " Try again later."
    if not authed:
        msg += " Add a token in Settings to raise the limit."
    return RateLimited(msg, reset_at)


def _get(client: httpx.Client, path: str, params: dict | None = None) -> httpx.Response:
    """GET one API path. Raises a GitHubError for every failure."""
    try:
        r = client.get(path, params=params)
    except httpx.HTTPError as exc:
        # Only the exception type: the text of some httpx errors holds the URL.
        raise NetworkError(f"{MSG_NETWORK} ({type(exc).__name__})") from exc
    if r.status_code < 400:
        return r
    authed = "Authorization" in client.headers
    limited = _rate_limit_error(r, authed)
    if limited:
        raise limited
    if r.status_code == 404:
        raise NotFound("GitHub answered: not found.")
    if r.status_code == 401:
        raise TokenRejected(MSG_REJECTED)
    detail = _api_message(r)
    raise GitHubError(
        f"GitHub answered with HTTP {r.status_code}" + (f": {detail}" if detail else ".")
    )


def _json(r: httpx.Response) -> dict | list:
    try:
        return r.json()
    except ValueError as exc:
        raise GitHubError("GitHub sent an answer that is not JSON.") from exc


def _paginate(client: httpx.Client, path: str, params: dict) -> Iterator[dict]:
    """Yield every item of a paginated list endpoint."""
    page = 1
    while True:
        batch = _json(_get(client, path, {**params, "per_page": PER_PAGE, "page": page}))
        if not isinstance(batch, list):
            raise GitHubError("GitHub sent an answer that is not a list.")
        yield from batch
        if len(batch) < PER_PAGE:
            return
        page += 1


def _repo_dict(repo: dict) -> dict:
    return {
        "full_name": repo["full_name"],
        "private": 1 if repo.get("private") else 0,
        "fork": 1 if repo.get("fork") else 0,
        "archived": 1 if repo.get("archived") else 0,
        "default_branch": repo.get("default_branch"),
        "has_wiki": bool(repo.get("has_wiki")),
        # Shown in the setup wizard; not stored.
        "owner": (repo.get("owner") or {}).get("login")
                 or repo["full_name"].split("/")[0],
        "description": repo.get("description") or "",
        "size_kb": repo.get("size") or 0,
        "pushed_at": repo.get("pushed_at") or "",
    }


# --- token check ------------------------------------------------------------

@dataclass
class TokenCheck:
    """Result of one `GET /user` call. Holds no secret."""
    ok: bool
    checked_at: float
    token_type: str = "unknown"        # 'classic' | 'fine-grained' | 'unknown'
    error_kind: str = ""               # 'rejected' | 'network' | 'other'
    error: str = ""                    # plain message for the UI
    login: str = ""
    name: str = ""
    scopes: list[str] | None = None    # None = GitHub sent no scope header
    expires_at: float | None = None    # epoch seconds; None = no expiry header


def token_type_of(token: str) -> str:
    """Tell the token type from its prefix."""
    if token.startswith("github_pat_"):
        return "fine-grained"
    if token.startswith("ghp_"):
        return "classic"
    return "unknown"


def _parse_expiry(value: str | None) -> float | None:
    """Parse `github-authentication-token-expiration` (e.g. '2026-12-31 23:59:59 UTC')."""
    if not value:
        return None
    value = value.strip()
    if value.endswith(" UTC"):
        try:
            parsed = datetime.strptime(value[:-4], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC).timestamp()
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z").timestamp()
    except ValueError:
        return None


def check_token(token: str) -> TokenCheck:
    """Ask GitHub who the token belongs to.

    Args:
        token: The token to check. It is sent to the GitHub API only.

    Returns:
        The account, the token type, the scopes (classic tokens) and the
        expiry date when GitHub reports one. On failure, `ok` is False and
        `error` holds a plain message. This function does not raise for
        network or HTTP errors.
    """
    token = (token or "").strip()
    result = TokenCheck(ok=False, checked_at=time.time(), token_type=token_type_of(token))
    if not token:
        result.error_kind, result.error = "rejected", "Enter a token."
        return result
    if not token.isascii() or not token.isprintable() or " " in token:
        # Such a value cannot be an HTTP header, so do not send it.
        result.error_kind = "rejected"
        result.error = "The token has characters that are not valid. Paste the token again."
        return result
    try:
        with _client(token) as client:
            r = client.get("/user")
    except httpx.HTTPError:
        result.error_kind, result.error = "network", MSG_NETWORK
        return result
    if r.status_code == 401:
        result.error_kind, result.error = "rejected", MSG_REJECTED
        return result
    if r.status_code != 200:
        result.error_kind = "other"
        result.error = (f"GitHub answered with HTTP {r.status_code}. "
                        "The token can be blocked or rate-limited. Try again later.")
        return result
    try:
        data = r.json()
    except ValueError:
        result.error_kind, result.error = "other", "GitHub sent an answer that is not valid JSON."
        return result

    result.ok = True
    result.login = str(data.get("login") or "")
    result.name = str(data.get("name") or "")
    scopes = r.headers.get("X-OAuth-Scopes")
    if scopes is not None:
        result.scopes = [s.strip() for s in scopes.split(",") if s.strip()]
    result.expires_at = _parse_expiry(r.headers.get("github-authentication-token-expiration"))
    return result


# --- discovery --------------------------------------------------------------

def list_all_repos(include_forks: bool, include_archived: bool) -> list[dict]:
    """List the repositories that the token can see (`GET /user/repos`).

    Raises:
        GitHubError: No token is configured, or the API call failed.
    """
    if not runtime.github_token():
        raise GitHubError("No GitHub token is configured")

    params = {
        "affiliation": "owner,collaborator,organization_member",
        "visibility": "all",
        "sort": "full_name",
    }
    out: list[dict] = []
    with _client() as client:
        for repo in _paginate(client, "/user/repos", params):
            if repo.get("fork") and not include_forks:
                continue
            if repo.get("archived") and not include_archived:
                continue
            out.append(_repo_dict(repo))
    return out


def get_repo(owner: str, name: str) -> dict:
    """Read one repository (`GET /repos/{owner}/{repo}`).

    The `full_name` in the result is the canonical one: GitHub ignores case
    and redirects a renamed repository.

    Raises:
        NotFound: The repository does not exist or the token cannot see it.
        GitHubError: Any other API failure, for example `RateLimited`.
    """
    with _client() as client:
        data = _json(_get(client, f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"))
    if not isinstance(data, dict) or "full_name" not in data:
        raise GitHubError("GitHub sent an answer that is not a repository.")
    return _repo_dict(data)


def get_owner(login: str) -> dict:
    """Read one account (`GET /users/{login}`).

    Returns:
        `{"login": canonical login, "kind": "user" or "org"}`.

    Raises:
        NotFound: No user or organisation has this login.
        GitHubError: Any other API failure.
    """
    with _client() as client:
        data = _json(_get(client, f"/users/{quote(login, safe='')}"))
    if not isinstance(data, dict) or "login" not in data:
        raise GitHubError("GitHub sent an answer that is not an account.")
    kind = "org" if data.get("type") == "Organization" else "user"
    return {"login": data["login"], "kind": kind}


def list_owner_repos(login: str, kind: str) -> list[dict]:
    """List the public repositories of one user or organisation.

    Args:
        login: Account name.
        kind: "user" or "org", from `get_owner`.

    Returns:
        Dicts with the same keys as `list_all_repos`. Forks and archived
        repositories are included; the caller filters them.
    """
    safe = quote(login, safe="")
    if kind == "org":
        path, params = f"/orgs/{safe}/repos", {"type": "public", "sort": "full_name"}
    else:
        path, params = f"/users/{safe}/repos", {"type": "owner", "sort": "full_name"}
    with _client() as client:
        # A token of an organisation member can make private repositories
        # show up here. This feature is for public repositories only.
        return [_repo_dict(r) for r in _paginate(client, path, params) if not r.get("private")]
