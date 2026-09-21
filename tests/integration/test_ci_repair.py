"""Integration: CI failure feeds back into repair (interpret + routing)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))

from contributor.github.ci import interpret_ci
from contributor.graph.routing import after_ci


def test_ci_fail_routes_to_debug():
    ci = interpret_ci({"combined": {"state": "failure", "statuses": [{"context": "t", "state": "failure"}]}, "check_runs": []})
    assert ci.state == "fail"
    assert after_ci({"ci_result": {"state": "fail"}}) == "debug"


def test_ci_pass_routes_to_wait():
    assert after_ci({"ci_result": {"state": "pass"}}) == "wait_for_review"
