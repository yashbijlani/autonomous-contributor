"""GitHub re-exports."""
from contributor.github.ci import (
    classify_ci_failure,
    fetch_ci,
    interpret_ci,
    watch_ci,
)
from contributor.github.client import GitHubClient, GitHubClientProtocol, RateLimitedError
from contributor.github.issues import fetch_issue, normalize_issue
from contributor.github.pull_requests import (
    PRTarget,
    create_pr_for_job,
    find_existing_pr,
    publish_pr,
    render_pr_body,
    resolve_pr_target,
    update_pr_body,
)

__all__ = [
    "GitHubClient", "GitHubClientProtocol", "RateLimitedError", "fetch_issue",
    "normalize_issue", "create_pr_for_job", "render_pr_body", "update_pr_body",
    "fetch_ci", "interpret_ci", "watch_ci", "classify_ci_failure",
    "publish_pr", "find_existing_pr", "resolve_pr_target", "PRTarget",
]
