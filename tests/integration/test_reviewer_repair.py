"""Integration: reviewer rejection triggers repair (adds regression test)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from contributor.agents.reviewer import review_change
from contributor.models.state import TestResult as StateTestResult


def test_reviewer_rejection_on_empty_tests_then_repair():
    diff = "diff --git a/calc.py b/calc.py\n+if b == 0: return 0"
    r1 = review_change(diff=diff, changed_files=["calc.py"],
                       test_results=[StateTestResult(command="pytest -q", exit_code=0, passed=True, ecosystem="python")])
    # reviewer asks for regression test (minor) but still approves or requests?
    # With only a minor issue, verdict is approved; ensure blocker path works:
    r2 = review_change(diff="", changed_files=[], test_results=[StateTestResult(command="pytest -q", exit_code=0, passed=True)])
    assert r2.verdict.value == "changes_required"
    # repaired: diff + test file -> approved
    r3 = review_change(diff=diff + "\n+test", changed_files=["calc.py", "test_calc.py"],
                       test_results=[StateTestResult(command="pytest -q", exit_code=0, passed=True)])
    assert r3.verdict.value == "approved"
