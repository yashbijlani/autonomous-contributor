"""Issue helpers: fetch + normalize + parse OWNER/REPO#123."""
from __future__ import annotations

from typing import Any

from contributor.github.client import GitHubClient
from contributor.models.state import IssueRef


def fetch_issue(client: GitHubClient, ref: IssueRef) -> dict[str, Any]:
    data = client.get_issue(ref.owner, ref.repo, ref.number)
    return data


def normalize_issue(data: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    title = str(data.get("title", ""))
    body = str(data.get("body", "") or "")
    meta = {
        "labels": [l.get("name") for l in data.get("labels", []) if isinstance(l, dict)],
        "state": data.get("state"),
        "user": (data.get("user") or {}).get("login"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "comments": data.get("comments", 0),
        "assignees": [a.get("login") for a in data.get("assignees", []) if isinstance(a, dict)],
    }
    return title, body, meta
