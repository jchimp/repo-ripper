"""Incremental metadata capture for one repository.

The first run downloads everything. Later runs ask GitHub only for the issues
and pull requests that changed after the saved cursor.

How the sync stays correct:

- The issue list is sorted by `updated_at`, oldest first. After each item the
  cursor in state.json moves to that item's `updated_at`. A run that stops (a
  crash, a rate limit) loses at most the item in work.
- `since` is inclusive and items can share a timestamp, so GitHub sends the
  items at the cursor again. An item is skipped only when its stored copy was
  downloaded clearly later than its `updated_at` (GitHub server time, from the
  `Date` header). A change after the download gives a newer `updated_at`, so
  it cannot hide behind the skip. In doubt the item is downloaded again.
- The list changes while the run reads it (an updated item moves to the end,
  and page 2 then starts one item late). So the run does not go to "page 2":
  after each full page it asks again with `since` = the newest timestamp seen.
  Only a full page of items with one identical timestamp uses the next link.
- An item that fails with an error that is not a rate limit goes to
  `state["failed"]` and is tried first on the next run. The cursor moves on,
  so one bad item cannot block the repository.
"""
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import store
from .client import PER_PAGE, GitHubClient, GitHubError, NotFound, RateLimited, format_time

# An item is skipped only when the stored copy is at least this much newer than
# the item's updated_at. Timestamps have a resolution of one second, and two
# GitHub servers can differ a little.
_SKIP_MARGIN = timedelta(seconds=2)
# After this many failed items in a row the cause is not one bad item
# (GitHub is down, the network is gone): stop the repository.
_MAX_FAILS_IN_A_ROW = 3


@dataclass
class CaptureResult:
    """Result of one capture run for one repository."""
    status: str                    # ok | partial | rate_limited | error
    message: str
    issues: int | None = None      # issues stored on disk (None: not known)
    pulls: int | None = None       # pull requests stored on disk (None: not known)
    fetched: int = 0               # items downloaded in this run
    skipped: int = 0               # items in the list that were already current
    reset_at: float | None = None  # Unix time, only for rate_limited


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _now() -> str:
    return _iso(datetime.now(UTC)) or ""


def _light_commit(commit: dict) -> dict:
    """Keep the parts of a pull request commit that a reader needs.

    The full commit is in the git mirror (`refs/pull/<n>/head`); this is only
    the list. The GitHub field names are kept.
    """
    inner = commit.get("commit") or {}
    author = commit.get("author")
    return {
        "sha": commit.get("sha"),
        "html_url": commit.get("html_url"),
        "commit": {
            "message": inner.get("message"),
            "author": inner.get("author"),
            "committer": inner.get("committer"),
        },
        "author": {"login": author.get("login")} if isinstance(author, dict) else None,
    }


class _TooManyFailures(Exception):
    """Several items in a row failed. Holds the last error message."""


class _Run:
    """State of one capture run. Private to this module."""

    def __init__(self, client: GitHubClient, full_name: str) -> None:
        self.client = client
        self.full_name = full_name
        self.api = f"/repos/{full_name}"
        self.base = store.meta_dir(full_name)
        self.state: dict = {}
        self.index: dict[tuple[str, int], dict] = {}
        self.fetched = 0
        self.skipped = 0
        self.fails_in_a_row = 0

    # --- state and index ----------------------------------------------------

    def open(self) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        store.remove_temp_files(self.full_name)
        store.write_readme(self.full_name)
        self.state = store.load_state(self.full_name)
        # A set flag means the last run did not reach its end (a crash), so
        # index.json can be older than the item files. Build it from the files.
        crashed = bool(self.state.get("run_in_progress"))
        entries = (store.rebuild_index(self.full_name)
                   if crashed or not store.has_metadata(self.full_name)
                   else store.load_index(self.full_name))
        self.index = {(e["kind"], e["number"]): e for e in entries
                      if e.get("kind") and isinstance(e.get("number"), int)}
        self.state.update({
            "format_version": store.FORMAT_VERSION,
            "full_name": self.full_name,
            "run_in_progress": True,
            "last_run_started_at": _now(),
            "unavailable": {},
        })
        self.state.setdefault("cursor", None)
        self.state.setdefault("failed", {})
        self.save_state()

    def save_state(self) -> None:
        store.atomic_write_json(self.base / "state.json", self.state)

    def count(self, kind: str) -> int:
        return sum(1 for k, _ in self.index if k == kind)

    def close(self, status: str, message: str, reset_at: float | None) -> None:
        store.write_index(self.full_name, list(self.index.values()))
        self.state.update({
            "run_in_progress": False,
            "last_run_finished_at": _now(),
            "last_status": status,
            "last_message": message,
            "rate_limit_reset_at": _iso(datetime.fromtimestamp(reset_at, UTC))
            if reset_at else None,
            "counts": {
                "issues": self.count(store.KIND_ISSUE),
                "pulls": self.count(store.KIND_PULL),
            },
            "last_run": {"fetched": self.fetched, "skipped": self.skipped},
        })
        if status == "ok":
            self.state["last_complete_at"] = self.state["last_run_finished_at"]
        self.save_state()

    def unavailable(self, what: str, exc: NotFound) -> None:
        self.state["unavailable"][what] = exc.status

    # --- small resources ----------------------------------------------------

    def small_resources(self) -> None:
        try:
            repo = self.client.get_json(self.api)
        except NotFound as exc:
            raise GitHubError(
                "GitHub does not show this repository (HTTP 404). It was deleted or renamed, "
                "or the token cannot read it.", exc.status) from exc
        store.atomic_write_json(self.base / "repo.json", repo)
        self.state["issues_disabled"] = not repo.get("has_issues", True)

        for name, params in (("labels", None), ("milestones", {"state": "all"}),
                             ("releases", None)):
            try:
                data = self.client.get_all(f"{self.api}/{name}", params)
            except NotFound as exc:
                # Not an error (for example a repository with no releases
                # feature). An older file stays as it is.
                self.unavailable(name, exc)
                continue
            store.atomic_write_json(self.base / f"{name}.json", data)

    # --- one item -----------------------------------------------------------

    def _sub_list(self, number: int, path: str, expected: int | None = None) -> list[dict]:
        """Download one list that belongs to an item. A 404/410 gives an empty list."""
        if expected == 0:
            return []  # the parent object says the list is empty: save the request
        try:
            return self.client.get_all(f"{self.api}/{path}")
        except NotFound as exc:
            self.unavailable(path, exc)
            return []

    def fetch_item(self, kind: str, number: int, issue: dict | None,
                   as_of: datetime | None) -> None:
        """Download one issue or pull request with its lists and write its file.

        `as_of` is the GitHub server time before the first data of this item
        was read. It is stored as `fetched_at` and drives the skip decision.
        """
        try:
            if kind == store.KIND_PULL:
                pull = self.client.get_json(f"{self.api}/pulls/{number}")
                doc = {
                    "pull": pull,
                    "comments": self._sub_list(number, f"issues/{number}/comments",
                                               pull.get("comments")),
                    "review_comments": self._sub_list(number, f"pulls/{number}/comments",
                                                      pull.get("review_comments")),
                    "reviews": self._sub_list(number, f"pulls/{number}/reviews"),
                    "commits": [_light_commit(c) for c in self._sub_list(
                        number, f"pulls/{number}/commits", pull.get("commits"))],
                }
            else:
                if issue is None:
                    issue = self.client.get_json(f"{self.api}/issues/{number}")
                doc = {
                    "issue": issue,
                    "comments": self._sub_list(number, f"issues/{number}/comments",
                                               issue.get("comments")),
                }
        except NotFound as exc:
            # The item itself is gone (deleted between the list and now).
            self.unavailable(f"{store._KIND_DIRS[kind]}/{number}", exc)
            return
        doc["fetched_at"] = _iso(as_of)
        store.atomic_write_json(store.item_path(self.full_name, kind, number), doc)
        self.index[(kind, number)] = store.index_entry(kind, doc)
        self.fetched += 1

    def is_current(self, kind: str, number: int, updated_at: str | None) -> bool:
        """True when the stored copy is surely not older than the listed item."""
        entry = self.index.get((kind, number))
        fetched, updated = _parse((entry or {}).get("fetched_at")), _parse(updated_at)
        return bool(fetched and updated and fetched >= updated + _SKIP_MARGIN)

    def process(self, kind: str, number: int, updated_at: str | None,
                issue: dict | None, as_of: datetime | None) -> bool:
        """Download one listed item if needed. Record a failure and continue.

        Returns False when the stored copy was current and nothing was done.
        """
        key = f"{kind}/{number}"
        if key not in self.state["failed"] and self.is_current(kind, number, updated_at):
            self.skipped += 1
            return False
        try:
            self.fetch_item(kind, number, issue, as_of)
        except GitHubError as exc:
            self.state["failed"][key] = {"error": str(exc), "at": _now()}
            self.fails_in_a_row += 1
            if self.fails_in_a_row >= _MAX_FAILS_IN_A_ROW:
                self.save_state()
                raise _TooManyFailures(str(exc)) from exc
        else:
            self.state["failed"].pop(key, None)
            self.fails_in_a_row = 0
        return True

    def retry_failed(self) -> None:
        """Try the items again that failed in an earlier run."""
        for key in list(self.state["failed"]):
            kind, _, num = key.partition("/")
            if kind not in (store.KIND_ISSUE, store.KIND_PULL) or not num.isdigit():
                del self.state["failed"][key]
                continue
            # No list entry here, so the issue object is downloaded too.
            self.process(kind, int(num), None, None, self.client.server_time)
            self.save_state()

    # --- item lists ---------------------------------------------------------

    def sync_items(self) -> None:
        try:
            self._sync_from_issue_list()
            self.state["mode"] = "issues"
        except NotFound as exc:
            # Issues are off (HTTP 410) or the list is not there. Pull requests
            # can exist all the same, so read them from the pull request list.
            self.unavailable("issues", exc)
            self.state["mode"] = "pulls_only"
            self.save_state()
            try:
                self._sync_from_pull_list()
            except NotFound as exc2:
                self.unavailable("pulls", exc2)

    def _sync_from_issue_list(self) -> None:
        base_params = {"state": "all", "sort": "updated", "direction": "asc",
                       "per_page": PER_PAGE}
        since: str | None = self.state.get("cursor")
        url: str = f"{self.api}/issues"
        params: dict | None = {**base_params, **({"since": since} if since else {})}
        while True:
            items, next_url = self.client.get_page(url, params)
            as_of = self.client.server_time
            for item in items:
                kind = store.KIND_PULL if "pull_request" in item else store.KIND_ISSUE
                updated_at = item.get("updated_at")
                self.process(kind, item["number"], updated_at,
                             item if kind == store.KIND_ISSUE else None, as_of)
                if updated_at and updated_at > (self.state.get("cursor") or ""):
                    self.state["cursor"] = updated_at
                self.save_state()
            if not next_url:
                return
            newest = items[-1].get("updated_at") if items else None
            if newest and newest > (since or ""):
                since = newest
                url, params = f"{self.api}/issues", {**base_params, "since": since}
            else:
                # A full page with one timestamp: `since` cannot move forward.
                url, params = next_url, None

    def _sync_from_pull_list(self) -> None:
        # This list has no `since`. Sorted by creation time its order is
        # stable, so paging cannot skip an item. Each run reads the full list
        # and downloads only the pull requests that are not current.
        url: str | None = f"{self.api}/pulls"
        params: dict | None = {"state": "all", "sort": "created", "direction": "asc",
                               "per_page": PER_PAGE}
        while url:
            items, url = self.client.get_page(url, params)
            params = None
            as_of = self.client.server_time
            for item in items:
                # The full list comes back on each run: write state.json
                # only for the pull requests that needed work.
                if self.process(store.KIND_PULL, item["number"], item.get("updated_at"),
                                None, as_of):
                    self.save_state()


def capture_repo(client: GitHubClient, full_name: str) -> CaptureResult:
    """Download the new and changed metadata of one repository.

    The function does not raise for GitHub or disk problems: the result holds
    the status. On a rate limit the status is `rate_limited` and the caller
    must stop sending requests until `reset_at`.

    Args:
        client: An open GitHub client (shared by all repositories of a job).
        full_name: The repository as `owner/name`.

    Raises:
        ValueError: `full_name` is not a valid repository name.
    """
    run = _Run(client, full_name)
    status, problem, reset_at = "ok", "", None
    try:
        run.open()
        try:
            run.small_resources()
            run.retry_failed()
            run.sync_items()
        except RateLimited as exc:
            status, reset_at = "rate_limited", exc.reset_at
            problem = (f"GitHub rate limit reached. The limit resets at "
                       f"{format_time(exc.reset_at)}. The next run continues.")
        except _TooManyFailures as exc:
            status, problem = "error", f"Stopped after {_MAX_FAILS_IN_A_ROW} failed items: {exc}"
        except GitHubError as exc:
            status, problem = "error", str(exc)

        failed = sorted(run.state.get("failed", {}))
        if status == "ok" and failed:
            status = "partial"
            problem = (f"{len(failed)} item(s) failed and will be tried again: "
                       f"{', '.join(failed[:5])}{' ...' if len(failed) > 5 else ''}.")
        issues, pulls = run.count(store.KIND_ISSUE), run.count(store.KIND_PULL)
        message = (f"{issues} issues, {pulls} pull requests on disk. "
                   f"{run.fetched} downloaded in this run.")
        if problem:
            message = f"{problem} {message}"
        run.close(status, message, reset_at)
    except OSError as exc:
        # Disk problems (share not mounted, no permission, disk full). The
        # run-in-progress flag stays set if it was written, so the next run
        # builds the index from the files.
        return CaptureResult("error", f"Cannot write to {run.base}: {exc}",
                             fetched=run.fetched, skipped=run.skipped)
    return CaptureResult(status, message, issues, pulls, run.fetched, run.skipped, reset_at)
