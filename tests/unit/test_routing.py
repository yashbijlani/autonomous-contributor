"""Unit: routing decisions are deterministic and capped."""
from contributor.config import Settings
from contributor.graph import routing


def _s(**kw):
    base = {"triage_result": None, "test_results": [], "review_result": None,
            "ci_result": None, "implementation_attempt": 0, "debug_attempt": 0,
            "review_attempt": 0, "pull_request_url": "", "done": False, "current_state": ""}
    base.update(kw)
    return base


def test_after_triage():
    assert routing.after_triage(_s(triage_result={"decision": "accept"})) == "env_discovery"
    assert routing.after_triage(_s(triage_result={"decision": "reject"})) == "escalate"
    assert routing.after_triage(_s(triage_result={"decision": "human_required"})) == "escalate"


def test_after_environment():
    s = Settings()
    assert routing.after_environment(_s(environment_report={"strategy": "standard_docker", "container_compatible": True}), s) == "plan"
    assert routing.after_environment(_s(environment_report={"strategy": "incompatible", "container_compatible": False}), s) == "env_unsupported"
    # HOST_REQUIRED proceeds unless hard-gated.
    host = _s(environment_report={"strategy": "host_required", "container_compatible": False})
    assert routing.after_environment(host, Settings(env_hard_gate=False)) == "plan"
    assert routing.after_environment(host, Settings(env_hard_gate=True)) == "env_unsupported"


def test_after_test_pass_goes_to_review():
    s = Settings()
    assert routing.after_test(_s(test_results=[{"passed": True}]), s) == "review"


def test_after_test_fail_goes_to_debug_within_limits():
    s = Settings()
    assert routing.after_test(_s(test_results=[{"passed": False}], debug_attempt=0), s) == "debug"


def test_after_test_fail_escalates_at_cap():
    s = Settings(max_debug_attempts=1, max_implementation_attempts=1)
    st = _s(test_results=[{"passed": False}], debug_attempt=1, implementation_attempt=1)
    assert routing.after_test(st, s) == "escalate"


def test_after_review_approved_no_pr():
    s = Settings()
    assert routing.after_review(_s(review_result={"verdict": "approved"}), s) == "create_pr"


def test_after_review_approved_with_pr():
    s = Settings()
    assert routing.after_review(_s(review_result={"verdict": "approved"}, pull_request_url="http://x"), s) == "update_pr"


def test_after_review_changes_required_loops_then_escalates():
    s = Settings(max_review_cycles=2, max_implementation_attempts=3)
    assert routing.after_review(_s(review_result={"verdict": "changes_required"}, review_attempt=0), s) == "implement"
    assert routing.after_review(_s(review_result={"verdict": "changes_required"}, review_attempt=5), s) == "escalate"


def test_after_ci():
    assert routing.after_ci(_s(ci_result={"state": "fail"})) == "debug"
    assert routing.after_ci(_s(ci_result={"state": "pass"})) == "wait_for_review"
    assert routing.after_ci(_s(ci_result={"state": "unknown"})) == "wait_for_review"
