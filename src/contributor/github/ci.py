"""CI interpretation. Deterministic: GitHub check states decide, never the LLM."""
from __future__ import annotations

from typing import Any

from contributor.github.client import GitHubClient
from contributor.models.state import CIFailureClass, CIResult, EnvironmentReport

_INFRA_SIGNALS = ("cancelled", "canceled", "timed_out", "timeout", "action_required", "stale", "runner", "startup")
_ENV_SIGNALS = ("missing", "not found", "no such file", "install", "dependency", "dependencies",
                "apt", "pip", "npm", "setup-python", "setup-node", "unable to locate")
_CODE_SIGNALS = ("test", "build", "compile", "lint", "unit", "integration", "ci", "check")


def classify_ci_failure(ci: CIResult, report: EnvironmentReport | None = None) -> CIFailureClass:
    """Deterministic CI failure classification (never LLM-decided)."""
    if report is not None and not report.container_compatible:
        return CIFailureClass.ENVIRONMENT_FAILURE
    failed = [c for c in ci.checks if str(c.get("state")) == "fail"]
    names = " ".join(str(c.get("name", "")) for c in (failed or ci.checks)).lower()
    if any(s in names for s in _INFRA_SIGNALS):
        return CIFailureClass.CI_INFRASTRUCTURE_FAILURE
    if any(s in names for s in _ENV_SIGNALS):
        return CIFailureClass.ENVIRONMENT_FAILURE
    if any(s in names for s in _CODE_SIGNALS):
        return CIFailureClass.CODE_FAILURE
    return CIFailureClass.UNKNOWN


def interpret_ci(payload: dict[str, Any]) -> CIResult:
    combined = payload.get("combined") or {}
    check_runs = payload.get("check_runs") or []
    checks: list[dict[str, Any]] = []
    worst = "pass"
    combined_state = str(combined.get("state", "") or "").lower()
    for s in combined.get("statuses", []) or []:
        checks.append({"name": s.get("context"), "state": s.get("state")})
        if str(s.get("state", "")).lower() == "failure":
            worst = "fail"
    for cr in check_runs:
        name = cr.get("name")
        status = str(cr.get("status", "") or "").lower()  # queued/in_progress/completed
        conclusion = str(cr.get("conclusion", "") or "").lower()  # success/failure/...
        if status != "completed":
            checks.append({"name": name, "state": "pending"})
            if worst == "pass":
                worst = "pending"
        elif conclusion in ("success", "neutral", "skipped"):
            checks.append({"name": name, "state": "pass"})
        else:
            checks.append({"name": name, "state": "fail"})
            worst = "fail"
    if not checks:
        if combined_state == "success":
            worst = "pass"
        elif combined_state in ("failure", "error"):
            worst = "fail"
        elif combined_state == "pending":
            worst = "pending"
        else:
            worst = "unknown"
    summary = f"{len(checks)} checks: {worst}"
    # mypy: worst is one of pass|fail|pending|unknown
    state = worst if worst in ("pass", "fail", "pending", "unknown") else "unknown"
    return CIResult(state=state, checks=checks, summary=summary)  # type: ignore[arg-type]


def fetch_ci(client: GitHubClient, owner: str, repo: str, sha: str) -> CIResult:
    payload = client.get_ci_status(owner, repo, sha)
    return interpret_ci(payload)
