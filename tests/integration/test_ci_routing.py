"""Integration: CI failure classification routes deterministically."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from contributor.config import Settings
from contributor.github.ci import classify_ci_failure
from contributor.graph.routing import after_ci
from contributor.models.state import CIResult


def _ci(names):
    return CIResult(state="fail", checks=[{"name": n, "state": "fail"} for n in names], summary="fail")


def test_ci_code_failure_debugs_within_budget():
    s = Settings(max_ci_repair_cycles=2)
    ci = _ci(["unit tests"])
    cls = classify_ci_failure(ci)
    st = {"ci_result": ci.model_dump(), "ci_failure_class": cls.value, "ci_repair_attempt": 1}
    assert after_ci(st, s) == "debug"


def test_ci_code_failure_escalates_after_budget():
    s = Settings(max_ci_repair_cycles=1)
    ci = _ci(["unit tests"])
    cls = classify_ci_failure(ci)
    st = {"ci_result": ci.model_dump(), "ci_failure_class": cls.value, "ci_repair_attempt": 3}
    assert after_ci(st, s) == "escalate"


def test_ci_environment_failure_terminal():
    s = Settings()
    ci = _ci(["setup-node missing dependency"])
    cls = classify_ci_failure(ci)
    st = {"ci_result": ci.model_dump(), "ci_failure_class": cls.value, "ci_repair_attempt": 1}
    assert after_ci(st, s) == "environment_failure"


def test_ci_infrastructure_failure_escalates():
    s = Settings()
    ci = _ci(["runner startup cancelled"])
    cls = classify_ci_failure(ci)
    st = {"ci_result": ci.model_dump(), "ci_failure_class": cls.value, "ci_repair_attempt": 1}
    assert after_ci(st, s) == "escalate"


def test_ci_pass_waits():
    assert after_ci({"ci_result": {"state": "pass"}}) == "wait_for_review"
