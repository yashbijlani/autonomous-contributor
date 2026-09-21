"""Unit: provider failures must not consume implementation/debug attempts, and
environment failures must not enter the debug loop."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))

from fakes import FakeRunnerResult  # noqa: E402

from contributor.config import Settings  # noqa: E402
from contributor.graph.nodes import WorkflowContext, node_debug, node_implement  # noqa: E402
from contributor.graph.routing import after_test  # noqa: E402
from contributor.models.state import JobState  # noqa: E402
from contributor.persistence.database import Database  # noqa: E402


class _Runner:
    def __init__(self, result):
        self.result = result

    def run_with_prompt(self, prompt, *, workdir, model="", variant="", timeout=None):
        return self.result


def _ctx(result, **settings_kw):
    settings = Settings(database_url="sqlite:///:memory:", **settings_kw)
    db = Database(settings.database_url)
    return WorkflowContext(settings=settings, db=db, github=None, sandbox=None, runner=_Runner(result))


def test_provider_error_does_not_consume_implementation_attempt():
    ctx = _ctx(FakeRunnerResult(1, "", "Unexpected server error", error_kind="PROVIDER_SERVER_ERROR"))
    job = JobState(job_id="p1", repository="o/r", issue_number=1)
    ctx.repo.save_incremental(job)
    out = node_implement(job.model_dump(mode="python"), ctx)
    assert out["implementation_attempt"] == 0
    assert out["issue_metadata"].get("provider_blocked") == "PROVIDER_SERVER_ERROR"


def test_provider_error_does_not_consume_debug_attempt():
    ctx = _ctx(FakeRunnerResult(1, "", "Authentication failed", error_kind="AUTH_FAILURE"))
    job = JobState(job_id="p2", repository="o/r", issue_number=1)
    job.debug_attempt = 0
    ctx.repo.save_incremental(job)
    out = node_debug(job.model_dump(mode="python"), ctx)
    assert out["debug_attempt"] == 0
    assert out["issue_metadata"].get("provider_blocked") == "AUTH_FAILURE"


def test_successful_run_consumes_attempt():
    ctx = _ctx(FakeRunnerResult(0, "done", ""))
    job = JobState(job_id="p3", repository="o/r", issue_number=1)
    ctx.repo.save_incremental(job)
    out = node_implement(job.model_dump(mode="python"), ctx)
    assert out["implementation_attempt"] == 1


def test_environment_failure_routes_without_debug():
    s = Settings()
    host = {
        "test_results": [{"command": "bash test/all", "passed": False, "environment_related": True}],
        "environment_report": {"strategy": "host_required", "container_compatible": False},
        "implementation_attempt": 1, "debug_attempt": 0,
    }
    assert after_test(host, s) == "env_unsupported"
    deps = {
        "test_results": [{"command": "npm test", "passed": False, "environment_related": True}],
        "environment_report": {"strategy": "standard_docker", "container_compatible": True},
        "implementation_attempt": 1, "debug_attempt": 0,
    }
    assert after_test(deps, s) == "environment_failure"


def test_real_failure_still_debugs():
    s = Settings()
    st = {
        "test_results": [{"command": "pytest -q", "passed": False, "environment_related": False}],
        "environment_report": {"strategy": "standard_docker", "container_compatible": True},
        "implementation_attempt": 1, "debug_attempt": 0,
    }
    assert after_test(st, s) == "debug"
