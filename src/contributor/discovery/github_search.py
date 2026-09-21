"""Issue discovery: search GitHub for candidate issues. Discovery only produces
candidates — it never modifies repositories."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from contributor.github.client import GitHubClient


@dataclass
class DiscoveryFilters:
    language: str = ""
    label: list[str] = field(default_factory=list)
    min_stars: int = 0
    max_difficulty: int = 5  # informational; triage decides for real
    max_age_days: int = 0  # 0 = no age limit
    state: str = "open"
    keyword: str = ""
    repo: str = ""  # OWNER/REPO to restrict to
    unassigned_only: bool = False
    per_page: int = 20


def build_query(f: DiscoveryFilters) -> str:
    parts = [f"state:{f.state}", "type:issue"]
    if f.repo:
        parts.append(f"repo:{f.repo}")
    if f.language:
        parts.append(f"language:{f.language}")
    for lb in f.label:
        parts.append(f'label:"{lb}"')
    if f.keyword:
        parts.append(f.keyword)
    if f.unassigned_only:
        parts.append("no:assignee")
    if f.min_stars:
        parts.append(f"stars:>={f.min_stars}")
    if f.max_age_days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=f.max_age_days)).strftime("%Y-%m-%d")
        parts.append(f"created:>={cutoff}")
    return " ".join(parts)


def discover(client: GitHubClient, filters: DiscoveryFilters) -> list[dict]:
    q = build_query(filters)
    try:
        items = client.search_issues(q, per_page=filters.per_page)
    except Exception:
        return []
    out = []
    for it in items:
        if it.get("pull_request"):
            continue  # exclude PRs from issue search
        out.append(it)
    return out[: filters.per_page]
