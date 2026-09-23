"""Unit: deterministic outcome classification."""
from contributor.benchmark.classify import classify_outcome
from contributor.benchmark.models import Outcome
from contributor.models.state import (
    JobState,
    JobStatus,
    ReviewResult,
    ReviewVerdict,
    TestResult as StateTestResult,
    TriageDecision,
    TriageResult,
)


def _job(**kw) -> JobState:
    j = JobState(job_id="j", repository="o/r", issue_number=1)
    for k, v in kw.items():
        setattr(j, k, v)
    return j


def _approved():
    return ReviewResult(verdict=ReviewVerdict.APPROVED, summary="ok")


def _passed_test():
    return StateTestResult(command="pytest -q test_x.py", exit_code=0, passed=True, ecosystem="python")


def test_success_requires_approval_and_passing_tests():
    j = _job(
        current_state=JobStatus.DONE,
        done=True,
        review_result=_approved(),
        test_results=[_passed_test()],
        implementation_attempt=1,
    )
    assert classify_outcome(j) == Outcome.SUCCESS


def test_success_after_repair_when_debug_used():
    j = _job(
        current_state=JobStatus.DONE,
        done=True,
        review_result=_approved(),
        test_results=[_passed_test()],
        implementation_attempt=1,
        debug_attempt=1,
    )
    assert classify_outcome(j) == Outcome.SUCCESS_AFTER_REPAIR


def test_full_suite_code_failure_downgrades_to_test_failure():
    j = _job(
        current_state=JobStatus.DONE,
        done=True,
        review_result=_approved(),
        test_results=[_passed_test()],
        implementation_attempt=1,
    )
    assert classify_outcome(j, full_suite_result="code_failure") == Outcome.TEST_FAILURE


def test_full_suite_environment_failure_keeps_success():
    j = _job(
        current_state=JobStatus.DONE,
        done=True,
        review_result=_approved(),
        test_results=[_passed_test()],
        implementation_attempt=1,
    )
    assert classify_outcome(j, full_suite_result="environment_failure") == Outcome.SUCCESS


def test_provider_blocked():
    j = _job(current_state=JobStatus.BLOCKED_PROVIDER, done=True, escalated=True)
    assert classify_outcome(j) == Outcome.PROVIDER_BLOCKED


def test_provider_timeout_is_timeout():
    j = _job(current_state=JobStatus.BLOCKED_PROVIDER, done=True, escalated=True)
    j.issue_metadata["provider_blocked"] = "PROVIDER_TIMEOUT"
    assert classify_outcome(j) == Outcome.TIMEOUT


def test_repository_environment_incompatible():
    j = _job(current_state=JobStatus.REPOSITORY_ENVIRONMENT_INCOMPATIBLE, done=True, escalated=True)
    assert classify_outcome(j) == Outcome.ENVIRONMENT_INCOMPATIBLE


def test_environment_failure_maps_to_incompatible():
    j = _job(current_state=JobStatus.ENVIRONMENT_FAILURE, done=True, escalated=True)
    assert classify_outcome(j) == Outcome.ENVIRONMENT_INCOMPATIBLE


def test_test_failure_when_targeted_code_fails():
    j = _job(
        current_state=JobStatus.ESCALATE,
        done=True,
        escalated=True,
        test_results=[StateTestResult(command="pytest", exit_code=1, passed=False, failures=["assert 1 == 2"])],
    )
    assert classify_outcome(j) == Outcome.TEST_FAILURE


def test_env_related_test_failure_is_environment():
    j = _job(
        current_state=JobStatus.ESCALATE,
        done=True,
        escalated=True,
        test_results=[StateTestResult(command="pytest", exit_code=1, passed=False, environment_related=True)],
    )
    assert classify_outcome(j) == Outcome.ENVIRONMENT_INCOMPATIBLE


def test_triage_rejected():
    j = _job(
        current_state=JobStatus.ESCALATE,
        done=True,
        escalated=True,
        triage_result=TriageResult(decision=TriageDecision.REJECT, reason="blocked"),
    )
    assert classify_outcome(j) == Outcome.TRIAGE_REJECTED


def test_human_required_from_triage():
    j = _job(
        current_state=JobStatus.ESCALATE,
        done=True,
        escalated=True,
        triage_result=TriageResult(decision=TriageDecision.HUMAN_REQUIRED, reason="decision"),
    )
    assert classify_outcome(j) == Outcome.HUMAN_REQUIRED


def test_timeout_from_errors():
    j = _job(
        current_state=JobStatus.ESCALATE,
        done=True,
        escalated=True,
        errors=["opencode TIMEOUT after 1800s"],
    )
    assert classify_outcome(j) == Outcome.TIMEOUT


def test_resource_incompatible_state():
    j = _job(current_state=JobStatus.RESOURCE_INCOMPATIBLE, done=True, escalated=True)
    assert classify_outcome(j) == Outcome.RESOURCE_INCOMPATIBLE


def test_oom_test_failure_is_resource_incompatible():
    j = _job(
        current_state=JobStatus.ESCALATE,
        done=True,
        escalated=True,
        test_results=[StateTestResult(
            command="cargo test", exit_code=137, passed=False,
            stderr="error: memory exhausted while compiling",
        )],
    )
    assert classify_outcome(j) == Outcome.RESOURCE_INCOMPATIBLE


def test_implementation_failed_when_empty_diff_and_escalated():
    j = _job(current_state=JobStatus.ESCALATE, done=True, escalated=True)
    assert classify_outcome(j) == Outcome.IMPLEMENTATION_FAILED
