"""PR helpers: deterministic target resolution, reuse, body rendering.

The orchestrator owns PR creation. Head/base are resolved dynamically from the
push target and the upstream repository; the contributor's own login is never
hardcoded, and a PR is only ever opened against the issue repository.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contributor.config import Settings
from contributor.github.client import GitHubClient
from contributor.models.state import JobState

# Template candidates in GitHub's documented resolution order.
_PR_TEMPLATE_PATHS = (
    ".github/pull_request_template.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "pull_request_template.md",
    "PULL_REQUEST_TEMPLATE.md",
    "docs/pull_request_template.md",
)


@dataclass(frozen=True)
class PRTarget:
    base_repo: str
    base_branch: str
    head_owner: str  # empty when the branch lives in the base repository
    head_ref: str  # "owner:branch" for forks, "branch" for same-repo


def resolve_pr_target(
    state: JobState,
    *,
    push_target: str = "",
    base_branch: str = "main",
    authenticated_login: str = "",
) -> PRTarget:
    """Deterministic resolution of the PR head/base.

    - base is always the issue repository (never an arbitrary repo)
    - when the branch was pushed to a fork, head is ``<fork-owner>:<branch>``
    - when the branch was pushed to the base repository, head is ``<branch>``
    """
    base_repo = state.repository
    base_owner = base_repo.split("/", 1)[0] if "/" in base_repo else ""
    target = (push_target or base_repo).strip().strip("/")

    if target.lower() == base_repo.lower():
        return PRTarget(base_repo, base_branch, "", state.branch_name)

    fork_owner = target.split("/", 1)[0] if "/" in target else target
    if not fork_owner:
        fork_owner = authenticated_login
    if fork_owner and fork_owner.lower() == base_owner.lower():
        return PRTarget(base_repo, base_branch, "", state.branch_name)
    return PRTarget(base_repo, base_branch, fork_owner, f"{fork_owner}:{state.branch_name}")


def render_pr_title(state: JobState) -> str:
    return f"Fix #{state.issue_number}: {state.issue_title[:80]}".strip()


def _render_generated_body(state: JobState, settings: Settings, changed_files: list[str]) -> str:
    lines = [
        f"Fixes #{state.issue_number}",
        "",
        "## Summary",
        (state.plan.objective if state.plan else "Automated fix via autonomous-contributor."),
        "",
        "## Changes",
    ]
    if changed_files:
        for f in changed_files[:25]:
            lines.append(f"- `{f}`")
    elif state.plan:
        for f in state.plan.files_expected_to_change[:25]:
            lines.append(f"- `{f}`")
    else:
        lines.append("- (see diff)")

    lines += ["", "## Tests executed"]
    if state.test_results:
        for t in state.test_results[-5:]:
            status = "PASS" if t.passed else "FAIL"
            lines.append(f"- `{t.command}` → {status} (exit {t.exit_code}, {t.duration_s:.1f}s)")
    else:
        lines.append("- (no test results recorded)")

    targeted = state.issue_metadata.get("targeted_test_command") if isinstance(state.issue_metadata, dict) else ""
    if targeted:
        lines += [
            "",
            "> Only the change-relevant tests above were executed by the automated "
            "pipeline; the full repository suite was not run.",
        ]

    lines += ["", "## Environment"]
    env = state.environment_report
    eco = (state.bootstrap_report or {}).get("ecosystem") or (env.languages[0] if env and env.languages else "unknown")
    lines.append(f"- Ecosystem: {eco}")
    health = state.environment_health or {}
    versions = health.get("toolchain_versions") or {}
    if versions:
        lines.append("- Toolchain: " + ", ".join(f"{k} {v}" for k, v in versions.items()))
    if state.resource_profile:
        lines.append(f"- Sandbox resource profile: {state.resource_profile}")

    lines += ["", "## Known limitations"]
    notes: list[str] = []
    if state.human_escalation_reason and not state.merge_ready:
        notes.append(state.human_escalation_reason)
    if state.ci_repair_attempt:
        notes.append(f"{state.ci_repair_attempt} automated CI repair iteration(s) were applied.")
    if state.ci_result and state.ci_result.state == "unknown":
        notes.append("No GitHub CI checks were reported for this commit.")
    if not notes:
        notes.append("None identified by the automated pipeline; maintainer review requested.")
    lines += [f"- {n}" for n in notes]

    if settings.pr_ai_disclosure:
        lines += ["", "---", settings.pr_ai_disclosure_text]
    return "\n".join(lines)


def _load_template(client: GitHubClient | None, base_repo: str) -> str:
    if client is None or not base_repo:
        return ""
    owner, repo = base_repo.split("/", 1)
    for path in _PR_TEMPLATE_PATHS:
        try:
            content = client.get_file(owner, repo, path)
        except Exception:
            content = None
        if content and content.strip():
            return content
    return ""


def render_pr_body(
    state: JobState,
    settings: Settings,
    *,
    client: GitHubClient | None = None,
    base_repo: str = "",
    changed_files: list[str] | None = None,
) -> str:
    body = _render_generated_body(state, settings, list(changed_files or []))
    if settings.pr_use_template:
        template = _load_template(client, base_repo or state.repository)
        if template:
            state.pr_template_used = True
            # Never overwrite required template sections; append our report.
            return template.rstrip() + "\n\n---\n\n" + body
    return body


def find_existing_pr(
    client: GitHubClient, base_repo: str, head_ref: str
) -> dict[str, Any] | None:
    """Return an existing PR for this head (prefer open), or None."""
    if not base_repo or not head_ref:
        return None
    owner, repo = base_repo.split("/", 1)
    try:
        prs = client.list_pull_requests(owner, repo, head=head_ref, state="all")
    except Exception:
        return None
    if not prs:
        return None
    open_prs = [p for p in prs if str(p.get("state")) == "open"]
    return (open_prs or prs)[0]


def publish_pr(
    client: GitHubClient,
    state: JobState,
    settings: Settings,
    *,
    push_target: str = "",
    base_branch: str = "main",
    changed_files: list[str] | None = None,
) -> tuple[dict[str, Any], str, PRTarget]:
    """Create, reuse or update the PR for this contribution branch.

    Returns (data, action, target) where action is created|reused|updated.
    """
    base_owner, base_repo_name = state.repository.split("/", 1)
    login = ""
    try:
        login = str((client.get_authenticated_user() or {}).get("login", ""))
    except Exception:
        login = ""
    target = resolve_pr_target(
        state, push_target=push_target, base_branch=base_branch, authenticated_login=login
    )
    state.pr_head_owner = target.head_owner
    state.pr_head_branch = state.branch_name
    state.pr_base_repo = target.base_repo
    state.pr_base_branch = target.base_branch

    title = render_pr_title(state)
    state.pr_title = title
    body = render_pr_body(
        state, settings, client=client, base_repo=target.base_repo, changed_files=changed_files
    )

    # Reuse the PR we already track (resume) or one that exists for this head.
    existing_number = state.pull_request_number
    existing = None
    if not existing_number:
        existing = find_existing_pr(client, target.base_repo, target.head_ref)
        if existing:
            existing_number = int(existing.get("number", 0) or 0)
            state.pr_reused = True

    if existing_number:
        data = client.update_pr(base_owner, base_repo_name, existing_number, title=title, body=body)
        state.pull_request_number = existing_number
        state.pull_request_url = (
            (data or {}).get("html_url")
            or state.pull_request_url
            or (existing or {}).get("html_url", "")
        )
        return (data or existing or {"number": existing_number}), ("updated" if existing is None else "reused"), target

    data = client.create_pr(
        base_owner,
        base_repo_name,
        title=title,
        head=target.head_ref,
        base=target.base_branch,
        body=body,
    )
    state.pull_request_number = int((data or {}).get("number", 0) or 0)
    state.pull_request_url = str((data or {}).get("html_url", "") or "")
    return (data or {}), "created", target


# --- Backward-compatible wrappers -------------------------------------------------


def create_pr_for_job(client: GitHubClient, state: JobState, settings: Settings, *, base: str = "main") -> dict:
    data, _, _ = publish_pr(client, state, settings, base_branch=base)
    return data


def update_pr_body(client: GitHubClient, state: JobState, settings: Settings) -> dict:
    owner, repo = state.repository.split("/", 1)
    body = render_pr_body(state, settings, client=client, base_repo=state.repository)
    return client.update_pr(owner, repo, state.pull_request_number, body=body)
