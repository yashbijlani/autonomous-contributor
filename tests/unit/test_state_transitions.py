"""Unit: state transitions via triage->plan helpers; retry caps."""
from contributor.agents.routing import escalate_model
from contributor.config import Settings
from contributor.graph.routing import after_debug_check


def test_debug_cap():
    s = Settings(max_debug_attempts=2)
    assert after_debug_check({"debug_attempt": 1}, s) == "test"
    assert after_debug_check({"debug_attempt": 3}, s) == "escalate"


def test_escalation_terminal():
    s = Settings(opencode_model_standard="a", opencode_model_xhigh="b")
    assert escalate_model(s, "a") == "b"
    assert escalate_model(s, "b") is None
