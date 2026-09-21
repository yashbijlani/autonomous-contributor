"""Regression: provider failures are classified and stop the workflow (CASE A)."""
from contributor.graph.routing import after_implement
from contributor.models.state import JobState, JobStatus
from contributor.opencode.runner import (
    ERROR_PERMISSION_REJECTED,
    ERROR_PROVIDER_SERVER,
    PROVIDER_BLOCKING_ERRORS,
    detect_permission_rejection,
)


def test_after_implement_blocks_on_provider_failure():
    assert after_implement({"issue_metadata": {"provider_blocked": ERROR_PROVIDER_SERVER}}) == "blocked_provider"


def test_after_implement_continues_normally():
    assert after_implement({"issue_metadata": {}}) == "test"


def test_permission_rejection_detected():
    assert detect_permission_rejection("", "permission requested: external_directory (/*); auto-rejecting")
    assert detect_permission_rejection("The user rejected permission to use this specific tool call.", "")
    assert not detect_permission_rejection("done", "")


def test_provider_blocking_set_contains_server_error():
    assert ERROR_PROVIDER_SERVER in PROVIDER_BLOCKING_ERRORS
    assert ERROR_PERMISSION_REJECTED not in PROVIDER_BLOCKING_ERRORS


def test_blocked_provider_node_sets_state():
    from contributor.graph.nodes import node_blocked_provider

    st = JobState(job_id="bp", repository="o/r", issue_number=1)
    st.issue_metadata["provider_blocked"] = ERROR_PROVIDER_SERVER

    class FakeRepo:
        def get(self, job_id):
            return st

        def save_incremental(self, s):
            pass

    ctx = type("C", (), {"repo": FakeRepo()})()
    out = node_blocked_provider(st.model_dump(mode="python"), ctx)
    assert out["current_state"] == JobStatus.BLOCKED_PROVIDER
    assert out["escalated"] is True
