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
    # PR / CI lifecycle
    execution_mode: str
    allow_push: bool
    allow_create_pr: bool
    ci_monitor: bool
    commit_sha: str
    remote_ref_verified: bool
    remote_ref_sha: str
    pr_head_owner: str
    pr_head_branch: str
    pr_base_repo: str
    pr_base_branch: str
    pr_title: str
    pr_reused: bool
    pr_template_used: bool
    ci_checks: list[dict[str, Any]]
    ci_diagnosis: dict[str, Any] | None
    ci_raw_ref: str
    ci_wait_s: float
    ci_polls: int
    ci_retry_count: int
    repair_history: list[dict[str, Any]]
    merge_ready: bool
    auto_merge: bool
    errors: list[str]
    event_history: list[dict[str, Any]]
    created_at: str
    updated_at: str
    model_selections: dict[str, str]
    human_escalation_reason: str
    escalated: bool
    done: bool
