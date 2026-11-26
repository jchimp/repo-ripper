"""An in-memory GitHub REST API for `httpx.MockTransport`. Test fixture data only."""
import re
import time
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx

API = "https://api.github.com"


def user(login: str) -> dict:
    return {"login": login, "id": abs(hash(login)) % 10000}


class FakeRepo:
    """One repository: issues, pull requests and their lists."""

    def __init__(self, full_name: str) -> None:
        self.full_name = full_name
        self.has_issues = True
        self.issues: dict[int, dict] = {}        # number -> issue object (also for PRs)
        self.pulls: dict[int, dict] = {}         # number -> extra pull fields
        self.comments: dict[int, list[dict]] = {}
        self.review_comments: dict[int, list[dict]] = {}
        self.reviews: dict[int, list[dict]] = {}
        self.commits: dict[int, list[dict]] = {}
        self.labels = [{"name": "bug", "color": "d73a4a"}, {"name": "docs", "color": "0075ca"}]
        self.milestones = [{"number": 1, "title": "v1", "state": "closed"}]
        self.releases = [{"id": 1, "tag_name": "v1.0", "name": "First", "body": "Notes",
                          "assets": [{"name": "app.zip", "size": 123}]}]

    def add_issue(self, number: int, title: str, updated_at: str, *, state: str = "open",
                  comments: int = 0, labels: tuple[str, ...] = (), author: str = "ann") -> None:
        self.issues[number] = {
            "number": number, "title": title, "state": state, "user": user(author),
            "body": f"Body of #{number} with **Markdown** and ünïcode",
            "labels": [lb for lb in self.labels if lb["name"] in labels],
            "created_at": "2024-01-01T00:00:00Z", "updated_at": updated_at,
            "closed_at": updated_at if state == "closed" else None,
        }
        self.comments[number] = [
            {"id": number * 100 + i, "body": f"comment {i} on #{number}", "user": user("bob")}
            for i in range(comments)
        ]

    def add_pull(self, number: int, title: str, updated_at: str, *, state: str = "open",
                 merged: bool = False, draft: bool = False, comments: int = 0,
                 review_comments: int = 0, reviews: int = 0, commits: int = 1) -> None:
        self.add_issue(number, title, updated_at, state=state, comments=comments, author="cy")
        self.pulls[number] = {
            "merged": merged, "merged_at": updated_at if merged else None, "draft": draft,
            "head": {"sha": f"{number:040x}", "ref": "feature"}, "base": {"ref": "main"},
        }
        self.review_comments[number] = [
            {"id": number * 1000 + i, "body": f"review comment {i}", "path": "a.py",
             "user": user("dee")} for i in range(review_comments)
        ]
        self.reviews[number] = [
            {"id": number * 2000 + i, "state": "APPROVED", "body": "ok", "user": user("dee")}
            for i in range(reviews)
        ]
        self.commits[number] = [
            {"sha": f"{number * 10 + i:040x}", "html_url": f"https://github.com/x/commit/{i}",
             "node_id": "dropped-by-the-light-copy",
             "commit": {"message": f"Commit {i}", "tree": {"sha": "t"},
                        "author": {"name": "Cy", "email": "cy@example.com",
                                   "date": "2024-01-01T00:00:00Z"},
                        "committer": {"name": "Cy", "email": "cy@example.com",
                                      "date": "2024-01-01T00:00:00Z"}},
             "author": user("cy")} for i in range(commits)
        ]

    def touch(self, number: int, updated_at: str, comment: str | None = None) -> None:
        """Change an item: new `updated_at`, and one more comment if given."""
        self.issues[number]["updated_at"] = updated_at
        if comment:
            self.comments[number].append({"id": number * 100 + 99, "body": comment,
                                          "user": user("bob")})

    def issue_view(self, number: int) -> dict:
        obj = {**self.issues[number], "comments": len(self.comments[number])}
        if number in self.pulls:
            obj["pull_request"] = {"url": f"{API}/repos/{self.full_name}/pulls/{number}"}
        return obj

    def pull_view(self, number: int) -> dict:
        return {
            **self.issues[number], **self.pulls[number],
            "comments": len(self.comments[number]),
            "review_comments": len(self.review_comments[number]),
            "commits": len(self.commits[number]),
        }


class FakeGitHub:
    """The transport handler plus knobs for rate limits and errors."""

    def __init__(self) -> None:
        self.repos: dict[str, FakeRepo] = {}
        self.now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)  # the `Date` header
        self.requests: list[str] = []      # "path?query" of each request
        self.auth: list[str | None] = []   # Authorization header of each request
        self.budget: int | None = None     # requests left before a primary rate limit
        self.reset_at = int(time.time()) + 3600
        # path -> queue of responses to send first, as (status, headers, body).
        self.forced: dict[str, list[tuple[int, dict, object]]] = {}
        self.always: dict[str, int] = {}   # path -> status to send each time

    def add_repo(self, full_name: str) -> FakeRepo:
        self.repos[full_name] = FakeRepo(full_name)
        return self.repos[full_name]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def paths(self, contains: str = "") -> list[str]:
        return [r.split("?")[0] for r in self.requests if contains in r]

    # --- handler ------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(f"{path}?{request.url.query.decode()}")
        self.auth.append(request.headers.get("Authorization"))
        headers = {"Date": format_datetime(self.now, usegmt=True)}

        if self.forced.get(path):
            status, extra, body = self.forced[path].pop(0)
            return httpx.Response(status, headers={**headers, **extra}, json=body)
        if path in self.always:
            return httpx.Response(self.always[path], headers=headers,
                                  json={"message": "Not Found"})

        if self.budget is not None:
            headers["X-RateLimit-Reset"] = str(self.reset_at)
            if self.budget <= 0:
                headers["X-RateLimit-Remaining"] = "0"
                return httpx.Response(403, headers=headers,
                                      json={"message": "API rate limit exceeded for 1.2.3.4."})
            self.budget -= 1
            headers["X-RateLimit-Remaining"] = str(self.budget)

        match = re.match(r"^/repos/([^/]+/[^/]+)(?:/(.*))?$", path)
        repo = self.repos.get(match.group(1)) if match else None
        if repo is None:
            return httpx.Response(404, headers=headers, json={"message": "Not Found"})
        result = self._route(repo, match.group(2) or "", request)
        if result is None:
            return httpx.Response(404, headers=headers, json={"message": "Not Found"})
        if isinstance(result, tuple):
            return httpx.Response(result[0], headers=headers, json={"message": result[1]})
        if isinstance(result, list):
            return self._page(result, request, headers)
        return httpx.Response(200, headers=headers, json=result)

    def _route(self, repo: FakeRepo, rest: str, request: httpx.Request):
        q = request.url.params
        if rest == "":
            return {"full_name": repo.full_name, "has_issues": repo.has_issues,
                    "description": "Demo"}
        if rest in ("labels", "milestones", "releases"):
            return getattr(repo, rest)
        if rest == "issues":
            if not repo.has_issues:
                return (410, "Issues are disabled for this repo")
            since = q.get("since", "")
            items = [repo.issue_view(n) for n in repo.issues
                     if repo.issues[n]["updated_at"] >= since]
            return sorted(items, key=lambda i: (i["updated_at"], i["number"]))
        if rest == "pulls":
            return [repo.pull_view(n) for n in sorted(repo.pulls)]
        m = re.match(r"^(issues|pulls)/(\d+)(?:/(comments|reviews|commits))?$", rest)
        if not m:
            return None
        area, number, sub = m.group(1), int(m.group(2)), m.group(3)
        if number not in repo.issues or (area == "pulls" and number not in repo.pulls):
            return None
        if area == "issues":
            if sub == "comments":
                return repo.comments[number]
            return repo.issue_view(number) if sub is None else None
        if sub is None:
            return repo.pull_view(number)
        return {"comments": repo.review_comments, "reviews": repo.reviews,
                "commits": repo.commits}[sub][number]

    @staticmethod
    def _page(items: list, request: httpx.Request, headers: dict) -> httpx.Response:
        q = request.url.params
        per_page, page = int(q.get("per_page", 30)), int(q.get("page", 1))
        start = (page - 1) * per_page
        if start + per_page < len(items):
            nxt = request.url.copy_merge_params({"page": page + 1})
            headers = {**headers, "Link": f'<{nxt}>; rel="next", <{nxt}>; rel="last"'}
        return httpx.Response(200, headers=headers, json=items[start:start + per_page])
