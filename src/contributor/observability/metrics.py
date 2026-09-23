"""PR/CI lifecycle metrics for live contributions.

The headline metric is not "did the model produce a diff" but "how often does the
system produce a PR that passes CI without human intervention". Every rate is
computed from persisted, deterministic facts (job events + CI results), never
from model claims.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from contributor.models.state import JobState, JobStatus
from contributor.observability import events

_TERMINAL_CI = {JobStatus.CI_PASSED, JobStatus.CI_FAILED, JobStatus.CI_UNKNOWN,
                JobStatus.MERGE_READY, JobStatus.CI_REPAIR_EXHAUSTED}


def _t(state: JobState, *types: str) -> float | None:
    for ev in state.event_history:
        if ev.type in types:
            try:
                return datetime.fromisoformat(ev.at.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    return None


def _delta(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return max(0.0, b - a)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def job_metrics(state: JobState, usage: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Deterministic metrics for one job."""
    usage = usage or []
    t_issue = _t(state, events.JOB_CREATED, events.ISSUE_FETCHED)
    t_push = _t(state, events.PUSHED, events.REPAIR_PUSHED)
    t_pr = _t(state, events.PR_OPENED, events.PR_REUSED)
    t_ci_pass = _t(state, events.CI_PASSED)
    boot = state.bootstrap_report or {}
    pushes = sum(1 for e in state.event_history if e.type in (events.PUSHED, events.REPAIR_PUSHED))
    ci_state = state.ci_result.state if state.ci_result else ""
    return {
        "job_id": state.job_id,
        "repository": state.repository,
        "issue_number": state.issue_number,
        "final_state": state.current_state.value,
        "execution_mode": state.execution_mode,
        "pr_created": bool(state.pull_request_url),
        "pr_number": state.pull_request_number,
        "pr_url": state.pull_request_url,
        "pr_reused": state.pr_reused,
        "merge_ready": state.merge_ready,
        "ci_state": ci_state,
        "ci_failure_class": state.ci_failure_class.value if state.ci_failure_class else "",
        "ci_repair_attempts": state.ci_repair_attempt,
        "ci_repair_succeeded": bool(state.ci_repair_attempt and ci_state == "pass"),
        "ci_checks": len(state.ci_checks or []),
        "ci_wait_s": state.ci_wait_s,
        "ci_polls": state.ci_polls,
        "ci_retries": state.ci_retry_count,
        "review_cycles": state.review_attempt,
        "implementation_attempts": state.implementation_attempt,
        "debug_attempts": state.debug_attempt,
        "push_count": pushes,
        "time_issue_to_push_s": _delta(t_issue, t_push),
        "time_issue_to_pr_s": _delta(t_issue, t_pr),
        "time_issue_to_ci_pass_s": _delta(t_issue, t_ci_pass),
        "bootstrap_time_s": float(boot.get("duration_s") or 0.0),
        "environment_cache_hit": bool(state.issue_metadata.get("env_cache_hit")),
        "resource_profile": state.resource_profile,
        "model_calls": len(usage),
        "model_seconds": round(sum(float(u.get("duration_s") or 0.0) for u in usage), 1),
        "errors": state.errors[-5:],
    }


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def aggregate_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-job metric dicts into the headline rates."""
    rows = list(rows)
    n = len(rows)
    with_pr = [r for r in rows if r.get("pr_created")]
    ci_rows = [r for r in rows if r.get("ci_state")]
    ci_pass = [r for r in ci_rows if r.get("ci_state") == "pass"]
    ci_fail = [r for r in ci_rows if r.get("ci_state") == "fail"]
    repaired = [r for r in rows if r.get("ci_repair_attempts")]
    repair_ok = [r for r in repaired if r.get("ci_repair_succeeded")]
    repair_attempts = [float(r.get("ci_repair_attempts") or 0) for r in repaired]

    def _avg(key: str) -> float:
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else 0.0

    def _med(key: str) -> float:
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        return round(_median(vals), 2)

    return {
        "jobs": n,
        "pr_created": len(with_pr),
        "pr_creation_success_rate": _rate(len(with_pr), n),
        "pr_reused": sum(1 for r in rows if r.get("pr_reused")),
        "ci_evaluated": len(ci_rows),
        "ci_passed": len(ci_pass),
        "ci_failed": len(ci_fail),
        "ci_pass_rate": _rate(len(ci_pass), len(ci_rows)),
        "ci_failure_rate": _rate(len(ci_fail), len(ci_rows)),
        "merge_ready": sum(1 for r in rows if r.get("merge_ready")),
        "ci_repair_rate": _rate(len(repaired), n),
        "ci_repair_successes": len(repair_ok),
        "ci_repair_success_rate": _rate(len(repair_ok), len(repaired)),
        "avg_repair_attempts": round(sum(repair_attempts) / len(repair_attempts), 2) if repair_attempts else 0.0,
        "median_repair_attempts": round(_median(repair_attempts), 2),
        "avg_ci_wait_s": _avg("ci_wait_s"),
        "median_ci_wait_s": _med("ci_wait_s"),
        "avg_time_issue_to_pr_s": _avg("time_issue_to_pr_s"),
        "median_time_issue_to_pr_s": _med("time_issue_to_pr_s"),
        "avg_time_issue_to_ci_pass_s": _avg("time_issue_to_ci_pass_s"),
        "median_time_issue_to_ci_pass_s": _med("time_issue_to_ci_pass_s"),
        "total_model_calls": sum(int(r.get("model_calls") or 0) for r in rows),
        "total_model_seconds": round(sum(float(r.get("model_seconds") or 0.0) for r in rows), 1),
        "total_bootstrap_time_s": round(sum(float(r.get("bootstrap_time_s") or 0.0) for r in rows), 1),
        "total_review_cycles": sum(int(r.get("review_cycles") or 0) for r in rows),
        "total_pushes": sum(int(r.get("push_count") or 0) for r in rows),
    }
