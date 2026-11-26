"""GitHub repository discovery and token check.

Uses the REST API with the authenticated-user token, so it sees everything the
token is scoped for: personal repos (public + private), collaborator repos, and
org repos where the user is a member.
"""
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from . import runtime
from .config import get_settings

# Tests set this to an `httpx.MockTransport`, so that no test uses the network.
_transport: httpx.BaseTransport | None = None

MSG_REJECTED = "GitHub rejected the token. The token is wrong or expired."
MSG_NETWORK = (
    "The app cannot reach api.github.com from the container. "
    "Check the network connection and the DNS of the Docker host."
)


def _client() -> httpx.Client:
    return httpx.Client(timeout=30, base_url=get_settings().github_api, transport=_transport)


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
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
        with _client() as client:
            r = client.get("/user", headers=_headers(token))
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
    token = runtime.github_token()
    if not token:
        raise RuntimeError("No GitHub token is configured")

    headers = _headers(token)
    params = {
        "per_page": 100,
        "affiliation": "owner,collaborator,organization_member",
        "visibility": "all",
        "sort": "full_name",
    }

    out: list[dict] = []
    with _client() as client:
        page = 1
        while True:
            params["page"] = page
            r = client.get("/user/repos", headers=headers, params=params)
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            for repo in batch:
                if repo.get("fork") and not include_forks:
                    continue
                if repo.get("archived") and not include_archived:
                    continue
                out.append({
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
                })
            if len(batch) < 100:
                break
            page += 1
    return out
