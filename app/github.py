"""GitHub repository discovery.

Uses the REST API with the authenticated-user token, so it sees everything the
token is scoped for: personal repos (public + private), collaborator repos, and
org repos where the user is a member.
"""
import httpx

from .config import get_settings


def list_all_repos(include_forks: bool, include_archived: bool) -> list[dict]:
    s = get_settings()
    if not s.github_token:
        raise RuntimeError("GITHUB_TOKEN is not set")

    headers = {
        "Authorization": f"Bearer {s.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    params = {
        "per_page": 100,
        "affiliation": "owner,collaborator,organization_member",
        "visibility": "all",
        "sort": "full_name",
    }

    out: list[dict] = []
    with httpx.Client(timeout=30, base_url=s.github_api) as client:
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
                })
            if len(batch) < 100:
                break
            page += 1
    return out
