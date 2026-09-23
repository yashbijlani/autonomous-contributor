"""Deterministic outcome classification for a benchmarked job.

Never trusts that a diff exists: success requires targeted tests to pass AND the
independent reviewer to approve. Environment failures are distinguished from
implementation/test failures.
"""
from __future__ import annotations

from contributor.benchmark.models import Outcome
from contributor.models.state import JobState, JobStatus, TriageDecision


def _is_timeout(job: JobState) -> bool:
    if any("TIMEOUT" in (e or "").upper() for e in job.errors):
        return True
    return any(t.timed_out for t in job.test_results)


def classify_outcome(
    job: JobState,
    *,
    full_suite_result: str = "not_run",
) -> Outcome:
    """Map a finished (benchmark-mode) JobState to a closed-set Outcome."""
    cs = job.current_state

    # Terminal environment / provider states are unambiguous.
    if cs == JobStatus.REPOSITORY_ENVIRONMENT_INCOMPATIBLE:
        return Outcome.ENVIRONMENT_INCOMPATIBLE
    if cs == JobStatus.ENVIRONMENT_FAILURE:
        return Outcome.ENVIRONMENT_INCOMPATIBLE
    if cs == JobStatus.RESOURCE_INCOMPATIBLE:
        return Outcome.RESOURCE_INCOMPATIBLE
    if cs == JobStatus.BLOCKED_PROVIDER:
        kind = str(job.issue_metadata.get("provider_blocked", "")).upper()
        return Outcome.TIMEOUT if "TIMEOUT" in kind else Outcome.PROVIDER_BLOCKED
    if cs == JobStatus.FAILED:
        return Outcome.TIMEOUT if _is_timeout(job) else Outcome.IMPLEMENTATION_FAILED

    # Triage-level outcomes.
    if job.triage_result is not None:
        if job.triage_result.decision == TriageDecision.REJECT:
            return Outcome.TRIAGE_REJECTED
        if job.triage_result.decision == TriageDecision.HUMAN_REQUIRED:
            return Outcome.HUMAN_REQUIRED

    targeted = job.test_results[-1] if job.test_results else None
    approved = bool(job.review_result and job.review_result.verdict.value == "approved")

    if approved and targeted is not None and targeted.passed:
        # Broader suite code failure means the change is not verified as safe.
        if full_suite_result == "code_failure":
            return Outcome.TEST_FAILURE
        repaired = (
            job.implementation_attempt > 1
            or job.debug_attempt > 0
            or job.review_attempt > 1
        )
        return Outcome.SUCCESS_AFTER_REPAIR if repaired else Outcome.SUCCESS

    if targeted is not None and not targeted.passed:
        if targeted.timed_out:
            return Outcome.TIMEOUT
        blob = f"{targeted.stdout}\n{targeted.stderr}".lower()
        if any(
            k in blob
            for k in ("out of memory", "cannot allocate memory", "memory exhausted", "oom-kill", "oom killed")
        ):
            return Outcome.RESOURCE_INCOMPATIBLE
        if targeted.environment_related:
            return Outcome.ENVIRONMENT_INCOMPATIBLE
        return Outcome.TEST_FAILURE

    if job.review_result is not None and job.review_result.verdict.value == "human_required":
        return Outcome.HUMAN_REQUIRED

    if job.escalated:
        reason = (job.human_escalation_reason or "").lower()
        if any(k in reason for k in ("maintainer", "human", "decision", "approval")):
            return Outcome.HUMAN_REQUIRED
        if _is_timeout(job):
            return Outcome.TIMEOUT
        return Outcome.IMPLEMENTATION_FAILED

    if _is_timeout(job):
        return Outcome.TIMEOUT
    return Outcome.IMPLEMENTATION_FAILED
