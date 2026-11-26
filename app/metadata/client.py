"""A small GitHub REST client for the metadata capture.

It works with or without a token (no token: public repositories only, 60
requests per hour). It follows `Link: rel="next"` pagination, tries again on
transient errors, and raises `RateLimited` on a rate limit so that the job can
stop and continue on the next run. The token is never logged and never put in
an exception message.
"""
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .. import runtime
from ..config import get_settings

# Tests put an `httpx.MockTransport` here. None = real network.
_transport: httpx.BaseTransport | None = None
# Tests replace this so that the backoff takes no time.
_sleep: Callable[[float], None] = time.sleep

PER_PAGE = 100
_RETRY_STATUS = {502, 503, 504}
_RETRIES = 3            # extra attempts after the first one
_BACKOFF_SECONDS = 2.0  # 2 s, 4 s, 8 s
_DEFAULT_WAIT = 60.0    # secondary limit that gives no Retry-After and no reset time


class GitHubError(Exception):
    """A request failed in a way that another attempt in this run will not fix."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotFound(GitHubError):
    """HTTP 404 or 410: the resource does not exist, is off, or is not visible."""


class RateLimited(Exception):
    """GitHub refused the request because of a primary or secondary rate limit.

    Attributes:
        reset_at: Unix time at which requests are permitted again.
    """

    def __init__(self, reset_at: float) -> None:
        self.reset_at = reset_at
        super().__init__(f"GitHub rate limit reached. It resets at {format_time(reset_at)}.")


def format_time(ts: float) -> str:
    """Format a Unix time as `YYYY-MM-DD HH:MM UTC` for messages."""
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


class GitHubClient:
    """One HTTP session to the GitHub API. Use it as a context manager."""

    def __init__(self) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "repo-ripper",
        }
        token = runtime.github_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._base = httpx.URL(get_settings().github_api)
        self._http = httpx.Client(
            base_url=self._base, headers=headers, timeout=30, transport=_transport,
            # A renamed repository answers with a redirect on the same host.
            # httpx removes the Authorization header if a redirect leaves the host.
            follow_redirects=True,
        )
        self.remaining: int | None = None   # X-RateLimit-Remaining of the last response
        self.reset_at: float | None = None  # X-RateLimit-Reset of the last response
        self.server_time: datetime | None = None  # Date header of the last response
        self.requests = 0

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP session."""
        self._http.close()

    # --- requests -----------------------------------------------------------

    def get(self, url: str, params: dict | None = None) -> httpx.Response:
        """GET one URL. Returns the response for a 2xx status.

        Raises:
            RateLimited: Primary or secondary rate limit.
            NotFound: HTTP 404 or 410.
            GitHubError: Any other failure, after the retries for transient errors.
        """
        # The last response said the budget is empty: do not send a request
        # that GitHub will refuse (repeated refused requests can extend a block).
        if self.remaining == 0 and self.reset_at and time.time() < self.reset_at:
            raise RateLimited(self.reset_at)

        last_problem = ""
        for attempt in range(_RETRIES + 1):
            if attempt:
                _sleep(_BACKOFF_SECONDS * 2 ** (attempt - 1))
            self.requests += 1
            try:
                resp = self._http.get(url, params=params)
            except httpx.TransportError as exc:
                # Timeouts and connection errors. The message has no headers in it.
                last_problem = f"{type(exc).__name__}: {exc}"
                continue
            self._note_headers(resp)
            if resp.status_code in _RETRY_STATUS:
                last_problem = f"HTTP {resp.status_code}"
                continue
            return self._check(resp)
        raise GitHubError(f"GitHub did not answer after {_RETRIES + 1} attempts ({last_problem})")

    def get_json(self, path: str, params: dict | None = None) -> Any:
        """GET one resource and return the parsed JSON body."""
        return self._json(self.get(path, params))

    def get_page(self, url: str, params: dict | None = None) -> tuple[list, str | None]:
        """GET one page of a list. Returns `(items, next_url)`; `next_url` is None on the last."""
        resp = self.get(url, params)
        data = self._json(resp)
        if not isinstance(data, list):
            raise GitHubError(f"GitHub sent an unexpected answer for {resp.request.url.path}")
        return data, self._next_url(resp)

    def paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        """Yield every item of a list endpoint, following `Link: rel="next"`."""
        url: str | None = path
        query: dict | None = {**(params or {}), "per_page": PER_PAGE}
        while url:
            items, url = self.get_page(url, query)
            query = None  # the next URL already holds the full query string
            yield from items

    def get_all(self, path: str, params: dict | None = None) -> list[dict]:
        """Return every item of a list endpoint as one list."""
        return list(self.paginate(path, params))

    # --- helpers ------------------------------------------------------------

    def _note_headers(self, resp: httpx.Response) -> None:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")
        if remaining is not None and remaining.isdigit():
            self.remaining = int(remaining)
        if reset is not None and reset.isdigit():
            self.reset_at = float(reset)
        date = resp.headers.get("Date")
        if date:
            try:
                self.server_time = parsedate_to_datetime(date).astimezone(UTC)
            except (TypeError, ValueError):
                self.server_time = None
        else:
            self.server_time = None

    def _check(self, resp: httpx.Response) -> httpx.Response:
        status = resp.status_code
        if 200 <= status < 300:
            return resp
        message = self._error_message(resp)
        if status in (403, 429):
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.strip().isdigit():
                # Secondary limit: GitHub says how long to wait.
                self.remaining, self.reset_at = 0, time.time() + int(retry_after)
                raise RateLimited(self.reset_at)
            if resp.headers.get("X-RateLimit-Remaining") == "0":
                self.remaining = 0
                self.reset_at = self.reset_at or time.time() + _DEFAULT_WAIT
                raise RateLimited(self.reset_at)
            if status == 429 or "rate limit" in message.lower():
                self.remaining, self.reset_at = 0, time.time() + _DEFAULT_WAIT
                raise RateLimited(self.reset_at)
        path = resp.request.url.path
        if status in (404, 410):
            raise NotFound(f"HTTP {status} for {path}", status)
        if status == 401:
            raise GitHubError("GitHub refused the token (HTTP 401). Check that it is valid "
                              "and not expired.", status)
        raise GitHubError(f"HTTP {status} for {path}: {message[:200]}", status)

    @staticmethod
    def _error_message(resp: httpx.Response) -> str:
        try:
            body = resp.json()
        except ValueError:
            return resp.text[:200]
        return str(body.get("message", "")) if isinstance(body, dict) else ""

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise GitHubError(
                f"GitHub sent a body that is not JSON for {resp.request.url.path}") from exc

    def _next_url(self, resp: httpx.Response) -> str | None:
        url = resp.links.get("next", {}).get("url")
        if not url:
            return None
        # The client sends the token to every URL it follows, so follow only
        # links that stay on the API host.
        target = httpx.URL(url)
        if target.is_absolute_url and (target.scheme, target.host, target.port) != (
                self._base.scheme, self._base.host, self._base.port):
            raise GitHubError("GitHub sent a next-page link to a different host. Not followed.")
        return url
