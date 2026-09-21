"""Graph state schema (TypedDict view over JobState for LangGraph)."""
from __future__ import annotations

from typing import Any, TypedDict


class GraphState(TypedDict, total=False):
    job_id: str
    repository: str
    issue_number: int
    issue_title: str
    issue_body: str
    issue_metadata: dict[str, Any]
    current_state: str
    triage_result: dict[str, Any] | None
    environment_report: dict[str, Any] | None
    plan: dict[str, Any] | None
    workspace_path: str
    branch_name: str
    implementation_attempt: int
    debug_attempt: int
    review_attempt: int
    test_results: list[dict[str, Any]]
    review_result: dict[str, Any] | None
    ci_result: dict[str, Any] | None
    ci_failure_class: str | None
    ci_repair_attempt: int
    pull_request_url: str
    pull_request_number: int
    errors: list[str]
    event_history: list[dict[str, Any]]
    created_at: str
    updated_at: str
    model_selections: dict[str, str]
    human_escalation_reason: str
    escalated: bool
    done: bool
