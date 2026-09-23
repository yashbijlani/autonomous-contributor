"""Event emission: persisted to DB + logged. Every important transition emits one."""
from __future__ import annotations

from typing import Any

from contributor.models.state import JobState
from contributor.observability.logging import get_logger

# Canonical event names used across the workflow.
JOB_CREATED = "JOB_CREATED"
ISSUE_FETCHED = "ISSUE_FETCHED"
TRIAGE_STARTED = "TRIAGE_STARTED"
TRIAGE_COMPLETED = "TRIAGE_COMPLETED"
ENVIRONMENT_DISCOVERY_STARTED = "ENVIRONMENT_DISCOVERY_STARTED"
ENVIRONMENT_DISCOVERY_COMPLETED = "ENVIRONMENT_DISCOVERY_COMPLETED"
ENVIRONMENT_STRATEGY = "ENVIRONMENT_STRATEGY"
MODEL_SELECTED = "MODEL_SELECTED"
MODEL_HEALTH_REUSED = "MODEL_HEALTH_REUSED"
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
USAGE_RECORDED = "USAGE_RECORDED"
PLAN_CREATED = "PLAN_CREATED"
WORKSPACE_CREATED = "WORKSPACE_CREATED"
IMPLEMENTATION_STARTED = "IMPLEMENTATION_STARTED"
IMPLEMENTATION_COMPLETED = "IMPLEMENTATION_COMPLETED"
TEST_STARTED = "TEST_STARTED"
TEST_FAILED = "TEST_FAILED"
TEST_PASSED = "TEST_PASSED"
REVIEW_STARTED = "REVIEW_STARTED"
REVIEW_REJECTED = "REVIEW_REJECTED"
REVIEW_APPROVED = "REVIEW_APPROVED"
REPAIR_STARTED = "REPAIR_STARTED"
PR_CREATED = "PR_CREATED"
PR_UPDATED = "PR_UPDATED"
CI_FAILED = "CI_FAILED"
CI_PASSED = "CI_PASSED"
HUMAN_ESCALATION = "HUMAN_ESCALATION"
JOB_COMPLETED = "JOB_COMPLETED"
JOB_FAILED = "JOB_FAILED"
PREFLIGHT_STARTED = "PREFLIGHT_STARTED"
PREFLIGHT_PASSED = "PREFLIGHT_PASSED"
PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
REPO_ENV_UNSUPPORTED = "REPO_ENV_UNSUPPORTED"
CI_CLASSIFIED = "CI_CLASSIFIED"
# Live contribution (orchestrator-owned push)
COMMIT_CREATED = "COMMIT_CREATED"
PUSH_GATE_PASSED = "PUSH_GATE_PASSED"
PUSH_GATE_REJECTED = "PUSH_GATE_REJECTED"
PUSH_STARTED = "PUSH_STARTED"
PUSHED = "PUSHED"
PUSH_FAILED = "PUSH_FAILED"


def emit(
    state: JobState,
    type: str,
    message: str = "",
    agent: str = "",
    model: str = "",
    attempt: int = 0,
    data: dict[str, Any] | None = None,
) -> None:
    state.add_event(type=type, message=message, agent=agent, model=model, attempt=attempt, data=data)
    log = get_logger("contributor.events")
    log.info(
        "%s job=%s repo=%s issue=%s %s",
        type,
        state.job_id,
        state.repository,
        state.issue_number,
        message,
    )
