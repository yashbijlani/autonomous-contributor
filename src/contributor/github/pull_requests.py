"""PR helpers: body rendering, create/update wrappers."""
from __future__ import annotations

from contributor.config import Settings
from contributor.github.client import GitHubClient
from contributor.models.state import JobState


def render_pr_body(state: JobState, settings: Settings) -> str:
    lines = [
        f"Fixes #{state.issue_number} — {state.issue_title}",
        "",
        "## Summary",
        (state.plan.objective if state.plan else "Automated fix via autonomous-contributor."),
        "",
        "## Implementation details",
    ]
    if state.plan:
        for s in state.plan.steps:
            lines.append(f"- {s}")
    lines += ["", "## Tests executed"]
    if state.test_results:
        for t in state.test_results[-3:]:
            status = "PASS" if t.passed else "FAIL"
            lines.append(f"- `{t.command}` → {status} (exit {t.exit_code}, {t.duration_s:.1f}s)")
    else:
        lines.append("- (no test results recorded)")
    lines += ["", "## Known limitations"]
    if state.human_escalation_reason:
        lines.append(f"- {state.human_escalation_reason}")
    else:
        lines.append("- None identified by automated pipeline; maintainer review requested.")
    if settings.pr_ai_disclosure:
        lines += ["", "---", settings.pr_ai_disclosure_text]
    return "\n".join(lines)


def create_pr_for_job(client: GitHubClient, state: JobState, settings: Settings, *, base: str = "main") -> dict:
    owner, repo = state.repository.split("/", 1)
    title = f"Fix #{state.issue_number}: {state.issue_title[:80]}"
    body = render_pr_body(state, settings)
    return client.create_pr(owner, repo, title=title, head=state.branch_name, base=base, body=body)


def update_pr_body(client: GitHubClient, state: JobState, settings: Settings) -> dict:
    owner, repo = state.repository.split("/", 1)
    return client.update_pr(owner, repo, state.pull_request_number, body=render_pr_body(state, settings))
