"""Unit: CI normalization, aggregation and deterministic failure classification."""
from contributor.github.ci import (
    apply_required,
    classify_ci_failure,
    extract_error_lines,
    interpret_ci,
)
from contributor.models.state import CIFailureClass, CIResult


def _ci(names, state="fail"):
    return CIResult(state=state, checks=[{"name": n, "state": state} for n in names], summary="x")


# --- normalization / aggregation -------------------------------------------------


def test_status_check_is_normalized():
    ci = interpret_ci({"combined": {"state": "failure", "statuses": [
        {"context": "ci/lint", "state": "failure", "target_url": "u"}]}, "check_runs": []})
    assert ci.state == "fail"
    assert ci.checks[0]["name"] == "ci/lint"
    assert ci.checks[0]["state"] == "fail"


def test_check_run_normalized_fields():
    payload = {"combined": {"state": "pending", "statuses": []}, "check_runs": [{
        "id": 5, "name": "build", "status": "completed", "conclusion": "success",
        "html_url": "https://x/5", "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:01:00Z", "app": {"name": "actions"},
        "check_suite": {"workflow_run": {"name": "CI"}},
    }]}
    ci = interpret_ci(payload)
    c = ci.checks[0]
    assert ci.state == "pass"
    assert c["workflow"] == "CI"
    assert c["duration_s"] == 60.0
    assert c["app"] == "actions"
    assert c["state"] == "pass"


def test_pending_ci():
    ci = interpret_ci({"combined": {"state": "pending", "statuses": []},
                       "check_runs": [{"name": "t", "status": "queued", "conclusion": None}]})
    assert ci.state == "pending"
    assert ci.phase == "queued"


def test_running_ci():
    ci = interpret_ci({"combined": {"state": "pending", "statuses": []},
                       "check_runs": [{"name": "t", "status": "in_progress", "conclusion": None}]})
    assert ci.state == "pending"
    assert ci.phase == "in_progress"


def test_success_ci():
    ci = interpret_ci({"combined": {"state": "success", "statuses": []},
                       "check_runs": [{"name": "t", "status": "completed", "conclusion": "success"}]})
    assert ci.state == "pass"


def test_failed_ci():
    ci = interpret_ci({"combined": {"state": "failure", "statuses": []},
                       "check_runs": [{"name": "t", "status": "completed", "conclusion": "failure"}]})
    assert ci.state == "fail"


def test_cancelled_conclusion_is_failure():
    ci = interpret_ci({"combined": {"state": "pending", "statuses": []},
                       "check_runs": [{"name": "t", "status": "completed", "conclusion": "cancelled"}]})
    assert ci.state == "fail"


def test_timed_out_conclusion_is_failure():
    ci = interpret_ci({"combined": {"state": "pending", "statuses": []},
                       "check_runs": [{"name": "t", "status": "completed", "conclusion": "timed_out"}]})
    assert ci.state == "fail"


def test_skipped_and_neutral_are_not_failures():
    ci = interpret_ci({"combined": {"state": "pending", "statuses": []}, "check_runs": [
        {"name": "a", "status": "completed", "conclusion": "skipped"},
        {"name": "b", "status": "completed", "conclusion": "neutral"},
    ]})
    assert ci.state == "pass"


def test_multiple_checks_worst_wins():
    ci = interpret_ci({"combined": {"state": "pending", "statuses": []}, "check_runs": [
        {"name": "a", "status": "completed", "conclusion": "success"},
        {"name": "b", "status": "completed", "conclusion": "failure"},
    ]})
    assert ci.state == "fail"
    assert ci.total == 2


def test_required_checks_marking():
    ci = interpret_ci({"combined": {"state": "pending", "statuses": []}, "check_runs": [
        {"name": "required", "status": "queued", "conclusion": None},
        {"name": "informational", "status": "completed", "conclusion": "success"},
    ]})
    apply_required(ci, ["required"])
    assert ci.required_total == 1
    assert ci.required_pending == 1
    assert ci.required_failed == 0


# --- classification --------------------------------------------------------------


def test_classify_code():
    assert classify_ci_failure(_ci(["unit tests"])) == CIFailureClass.CODE_FAILURE


def test_classify_test():
    assert classify_ci_failure(_ci(["pytest suite"])) == CIFailureClass.TEST_FAILURE


def test_classify_build():
    assert classify_ci_failure(_ci(["job"]), logs="cargo build failed: linker error") == CIFailureClass.BUILD_FAILURE


def test_classify_dependency():
    assert classify_ci_failure(_ci(["job"]), logs="Could not find package requests") == CIFailureClass.DEPENDENCY_FAILURE


def test_classify_environment():
    assert classify_ci_failure(_ci(["job"]), logs="workspace toolchain missing") == CIFailureClass.ENVIRONMENT_FAILURE


def test_classify_resource():
    assert classify_ci_failure(_ci(["job"]), logs="Killed process 42 (node) out of memory") == CIFailureClass.RESOURCE_FAILURE


def test_classify_network():
    assert classify_ci_failure(_ci(["job"]), logs="getaddrinfo ENOTFOUND registry.npmjs.org") == CIFailureClass.NETWORK_FAILURE


def test_classify_ci_infrastructure():
    assert classify_ci_failure(_ci(["job"]), logs="The runner has received a shutdown signal") == CIFailureClass.CI_INFRASTRUCTURE_FAILURE


def test_classify_permission():
    assert classify_ci_failure(_ci(["job"]), logs="Resource not accessible by personal access token") == CIFailureClass.PERMISSION_FAILURE


def test_classify_flaky():
    assert classify_ci_failure(_ci(["job"]), logs="flaky test detected; please retry") == CIFailureClass.FLAKY_FAILURE


def test_classify_unknown():
    assert classify_ci_failure(_ci(["something odd"])) == CIFailureClass.UNKNOWN


def test_repairable_set():
    assert CIFailureClass.CODE_FAILURE.repairable
    assert CIFailureClass.TEST_FAILURE.repairable
    assert CIFailureClass.BUILD_FAILURE.repairable
    assert not CIFailureClass.CI_INFRASTRUCTURE_FAILURE.repairable
    assert not CIFailureClass.RESOURCE_FAILURE.repairable
    assert not CIFailureClass.FLAKY_FAILURE.repairable


def test_extract_error_lines_bounded():
    text = "\n".join(f"line {i}" for i in range(100)) + "\nAssertionError: boom\nTraceback (most recent call last):\n"
    out = extract_error_lines(text, limit=5)
    assert any("AssertionError" in l for l in out)
    assert len(out) <= 5
