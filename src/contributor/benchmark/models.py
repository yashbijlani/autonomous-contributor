"""Typed benchmark domain models.

There is deliberately no aggregate "agent intelligence score": every metric is
recorded separately and aggregated only as explicit rates/averages.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from contributor.models.state import TriageAssessment, utcnow_iso


class Outcome(str, Enum):
    """Terminal classification for a benchmarked issue (closed set)."""

    SUCCESS = "SUCCESS"
    SUCCESS_AFTER_REPAIR = "SUCCESS_AFTER_REPAIR"
    IMPLEMENTATION_FAILED = "IMPLEMENTATION_FAILED"
    TEST_FAILURE = "TEST_FAILURE"
    ENVIRONMENT_INCOMPATIBLE = "ENVIRONMENT_INCOMPATIBLE"
    RESOURCE_INCOMPATIBLE = "RESOURCE_INCOMPATIBLE"
    PROVIDER_BLOCKED = "PROVIDER_BLOCKED"
    TRIAGE_REJECTED = "TRIAGE_REJECTED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    TIMEOUT = "TIMEOUT"


class BenchmarkConfig(BaseModel):
    repo: str
    count: int = 5
    workers: int = 1
    labels: list[str] = Field(default_factory=list)
    max_complexity: int = 3
    pool_size: int = 100
    # Force specific issue numbers (skip selection filters); used for re-validation.
    issues: list[int] = Field(default_factory=list)
    output_dir: str = "."
    full_suite: bool = True
    no_push: bool = True
    no_pr: bool = True
    preflight: bool = True
    dry_run: bool = False
    # Cheap-model triage pass over candidate bodies (deterministic pre-filter first).
    llm_triage: bool = True
    triage_cap: int = 30
    triage_timeout_s: int = 180


class CandidateInfo(BaseModel):
    """Discovery + triage + environment record for one candidate issue."""

    number: int
    title: str
    url: str
    labels: list[str] = Field(default_factory=list)
    assessment: TriageAssessment
    triage_decision: str = "unknown"
    triage_source: str = "deterministic"
    triage_model: str = ""
    triage_reason: str = ""
    selected: bool = False
    selection_reasons: list[str] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)
    environment_strategy: str = ""
    container_compatible: bool | None = None


class BenchmarkRecord(BaseModel):
    repository: str
    issue_number: int
    issue_title: str
    issue_url: str = ""
    job_id: str = ""
    selected: bool = False
    selection_reasons: list[str] = Field(default_factory=list)
    triage: dict[str, Any] = Field(default_factory=dict)
    assessment: TriageAssessment
    human_baseline: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)
    complexity: int = 0
    model_tier: str = ""
    model: str = ""
    variant: str = ""
    implementation_attempts: int = 0
    debug_attempts: int = 0
    review_cycles: int = 0
    targeted_tests: dict[str, Any] = Field(default_factory=dict)
    broader_tests: dict[str, Any] = Field(default_factory=dict)
    review: dict[str, Any] = Field(default_factory=dict)
    full_suite_result: str = "not_run"
    duration_s: float = 0.0
    # In-sandbox environment provisioning metrics.
    environment_bootstrap_time_s: float = 0.0
    environment_cache_hit: bool = False
    resource_profile: str = ""
    toolchain_versions: dict[str, str] = Field(default_factory=dict)
    bootstrap_commands: list[dict[str, Any]] = Field(default_factory=list)
    model_usage: list[dict[str, Any]] = Field(default_factory=list)
    estimated_cost: float | None = None
    outcome: Outcome
    workspace_path: str = ""
    diff_path: str = ""
    diff_lines: int = 0
    errors: list[str] = Field(default_factory=list)


class BenchmarkSummary(BaseModel):
    repository: str
    issues_attempted: int = 0
    successful: int = 0
    successful_after_repair: int = 0
    implementation_failures: int = 0
    test_failures: int = 0
    environment_failures: int = 0
    resource_failures: int = 0
    provider_failures: int = 0
    human_escalations: int = 0
    triage_rejected: int = 0
    timeouts: int = 0
    success_rate: float = 0.0
    success_after_repair_rate: float = 0.0
    implementation_failure_rate: float = 0.0
    environment_incompatibility_rate: float = 0.0
    resource_incompatibility_rate: float = 0.0
    provider_failure_rate: float = 0.0
    average_duration_s: float = 0.0
    median_duration_s: float = 0.0
    average_implementation_attempts: float = 0.0
    average_debug_attempts: float = 0.0
    model_usage_by_tier: dict[str, dict[str, Any]] = Field(default_factory=dict)
    generated_at: str = Field(default_factory=utcnow_iso)


class BenchmarkReport(BaseModel):
    config: BenchmarkConfig
    summary: BenchmarkSummary
    candidates: list[CandidateInfo] = Field(default_factory=list)
    records: list[BenchmarkRecord] = Field(default_factory=list)
