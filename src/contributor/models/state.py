"""Strongly-typed state and domain models."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStatus(str, Enum):
    CREATED = "created"
    DISCOVER = "discover"
    TRIAGE = "triage"
    ENVIRONMENT_DISCOVERY = "environment_discovery"
    PLAN = "plan"
    PREPARE_WORKSPACE = "prepare_workspace"
    IMPLEMENT = "implement"
    TEST = "test"
    REVIEW = "review"
    DEBUG = "debug"
    CREATE_PR = "create_pr"
    PR_CI_CHECK = "pr_ci_check"
    WAIT_FOR_REVIEW = "wait_for_review"
    UPDATE_PR = "update_pr"
    ESCALATE = "escalate"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # Provider could not perform inference (auth/model/network/config). Distinct
    # from implementation failure: retrying the workflow cannot help.
    BLOCKED_PROVIDER = "blocked_provider"
    # Worker ran, but the repo's tests cannot execute in this sandbox
    # (unknown ecosystem / missing runner). Distinct from test failure.
    REPOSITORY_ENVIRONMENT_INCOMPATIBLE = "repository_environment_incompatible"
    # Tests failed because dependencies/environment are missing (CASE E).
    ENVIRONMENT_FAILURE = "environment_failure"
    # The repository build genuinely cannot fit host resources.
    RESOURCE_INCOMPATIBLE = "resource_incompatible"
    # Live contribution: orchestrator pushed the contribution branch.
    PUSHED = "pushed"
    # Live contribution: push attempted and failed (distinct from impl failure).
    PUSH_FAILED = "push_failed"
    # Final deterministic gate refused to push (not a code failure).
    PUSH_GATE_REJECTED = "push_gate_rejected"


class TriageDecision(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    HUMAN_REQUIRED = "human_required"


class EnvironmentStrategy(str, Enum):
    STANDARD_DOCKER = "standard_docker"
    CUSTOM_DOCKER = "custom_docker"
    REPOSITORY_DEVCONTAINER = "repository_devcontainer"
    REPOSITORY_CI_ENVIRONMENT = "repository_ci_environment"
    HOST_REQUIRED = "host_required"
    INCOMPATIBLE = "incompatible"


class CIFailureClass(str, Enum):
    CODE_FAILURE = "code_failure"
    ENVIRONMENT_FAILURE = "environment_failure"
    CI_INFRASTRUCTURE_FAILURE = "ci_infrastructure_failure"
    UNKNOWN = "unknown"


class EnvironmentReport(BaseModel):
    languages: list[str] = Field(default_factory=list)
    package_managers: list[str] = Field(default_factory=list)
    build_systems: list[str] = Field(default_factory=list)
    test_frameworks: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    requires_systemd: bool = False
    requires_host_runtime: bool = False
    privileged_ops: bool = False
    unsafe_ops: bool = False
    network_required: bool = False
    container_compatible: bool = True
    strategy: EnvironmentStrategy = EnvironmentStrategy.STANDARD_DOCKER
    confidence: float = Field(default=0.3, ge=0.0, le=1.0)
    signals: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class ReviewVerdict(str, Enum):
    APPROVED = "approved"
    CHANGES_REQUIRED = "changes_required"
    HUMAN_REQUIRED = "human_required"


class IssueRef(BaseModel):
    owner: str
    repo: str
    number: int

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def short(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    @classmethod
    def parse(cls, text: str) -> IssueRef:
        """Parse 'OWNER/REPO#123'."""
        text = text.strip()
        if "#" not in text or "/" not in text:
            raise ValueError(f"Invalid issue ref {text!r}; expected OWNER/REPO#123")
        repo_part, num_part = text.rsplit("#", 1)
        owner, repo = repo_part.split("/", 1)
        return cls(owner=owner.strip(), repo=repo.strip(), number=int(num_part.strip()))


class TriageResult(BaseModel):
    decision: TriageDecision
    issue_type: Literal["bug", "feature", "docs", "refactor", "test", "chore", "security", "unknown"] = "unknown"
    difficulty: int = Field(default=3, ge=1, le=5)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    likely_files: list[str] = Field(default_factory=list)
    requires_human: bool = False
    reason: str = ""
    recommended_model: Literal["high", "xhigh"] = "high"
    security_flag: bool = False


class TriageAssessment(BaseModel):
    """Structured, factual solvability assessment for benchmark triage.

    Deliberately NOT an aggregate "quality score": every dimension is recorded
    separately so selection can apply transparent, configurable filters.
    Derived deterministically from the issue title/body/labels (facts, not
    guesses). Human-baseline dimensions are kept separate from solvability.
    """

    actionable: bool = False
    reproducible: bool = False
    clear_expected_behavior: bool = False
    likely_code_change: bool = False
    likely_test_change: bool = False
    container_testable: bool = False
    requires_maintainer_decision: bool = False
    requires_external_service: bool = False
    estimated_complexity: int = Field(default=3, ge=1, le=5)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    # Human-baseline dimensions (never collapsed into a single score).
    clear_reproduction: bool = False
    existing_relevant_tests: bool = False
    clear_location_in_code: bool = False
    obvious_acceptance_condition: bool = False
    signals: list[str] = Field(default_factory=list)


class ImplementationPlan(BaseModel):
    objective: str
    suspected_root_cause: str = ""
    files_to_inspect: list[str] = Field(default_factory=list)
    files_expected_to_change: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    tests_to_add_or_change: list[str] = Field(default_factory=list)
    commands_to_run: list[str] = Field(default_factory=list)
    potential_regressions: list[str] = Field(default_factory=list)
    rollback_notes: str = ""


class TestResult(BaseModel):
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    passed: bool = False
    failures: list[str] = Field(default_factory=list)
    ecosystem: str = "unknown"
    # Failure dominated by missing host tooling rather than the code change
    # (CASE B: REPOSITORY_ENVIRONMENT_INCOMPATIBLE).
    environment_related: bool = False


class ReviewIssue(BaseModel):
    severity: Literal["info", "minor", "major", "blocker"] = "minor"
    file: str = ""
    line: int | None = None
    message: str = ""


class ReviewResult(BaseModel):
    verdict: ReviewVerdict
    issues: list[ReviewIssue] = Field(default_factory=list)
    summary: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class CIResult(BaseModel):
    state: Literal["pass", "fail", "pending", "unknown"] = "unknown"
    checks: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""


class JobEvent(BaseModel):
    type: str
    at: str = Field(default_factory=utcnow_iso)
    attempt: int = 0
    agent: str = ""
    model: str = ""
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class JobState(BaseModel):
    job_id: str
    repository: str = ""  # OWNER/REPO
    issue_number: int = 0
    issue_title: str = ""
    issue_body: str = ""
    issue_metadata: dict[str, Any] = Field(default_factory=dict)
    current_state: JobStatus = JobStatus.CREATED
    triage_result: TriageResult | None = None
    environment_report: EnvironmentReport | None = None
    plan: ImplementationPlan | None = None
    workspace_path: str = ""
    branch_name: str = ""
    implementation_attempt: int = 0
    debug_attempt: int = 0
    review_attempt: int = 0
    test_results: list[TestResult] = Field(default_factory=list)
    review_result: ReviewResult | None = None
    ci_result: CIResult | None = None
    ci_repair_attempt: int = 0
    pull_request_url: str = ""
    pull_request_number: int = 0
    errors: list[str] = Field(default_factory=list)
    event_history: list[JobEvent] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow_iso)
    updated_at: str = Field(default_factory=utcnow_iso)
    model_selections: dict[str, str] = Field(default_factory=dict)
    ci_failure_class: CIFailureClass | None = None
    human_escalation_reason: str = ""
    escalated: bool = False
    done: bool = False
    # In-sandbox environment provisioning (bootstrap + preflight).
    bootstrap_report: dict[str, Any] | None = None
    environment_health: dict[str, Any] | None = None
    resource_profile: str = ""
    # Live contribution mode + orchestrator-owned push.
    execution_mode: str = "benchmark"  # benchmark | live
    commit_sha: str = ""
    push_remote: str = ""
    push_target: str = ""
    push_gate: dict[str, Any] | None = None
    push_result: dict[str, Any] | None = None

    def touch(self) -> None:
        self.updated_at = utcnow_iso()

    def add_event(
        self,
        type: str,
        message: str = "",
        agent: str = "",
        model: str = "",
        attempt: int = 0,
        data: dict[str, Any] | None = None,
    ) -> JobEvent:
        ev = JobEvent(
            type=type, message=message, agent=agent, model=model, attempt=attempt, data=data or {}
        )
        self.event_history.append(ev)
        self.touch()
        return ev

    def last_test_passed(self) -> bool | None:
        if not self.test_results:
            return None
        return self.test_results[-1].passed
