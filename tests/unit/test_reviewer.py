"""Unit: reviewer independence — diff + exit codes decide, not claims."""
from contributor.agents.reviewer import review_change
from contributor.models.state import TestResult as StateTestResult


def _tr(passed: bool) -> StateTestResult:
    return StateTestResult(command="pytest -q", exit_code=0 if passed else 1, passed=passed, ecosystem="python")


def test_empty_diff_blocked():
    r = review_change(diff="", changed_files=[], test_results=[_tr(True)])
    assert r.verdict.value == "changes_required"


def test_failing_tests_block():
    r = review_change(diff="diff --git a/x.py b/x.py\n+fix", changed_files=["x.py"], test_results=[_tr(False)])
    assert r.verdict.value == "changes_required"
    assert any(i.severity == "blocker" for i in r.issues)


def test_secret_in_diff_blocked():
    diff = "+key = 'AKIAIOSFODNN7EXAMPLE'\n context"
    r = review_change(diff=diff, changed_files=["x.py"], test_results=[_tr(True)])
    assert r.verdict.value == "changes_required"


def test_good_change_approved():
    diff = "diff --git a/fix.py b/fix.py\n+return 42"
    r = review_change(diff=diff, changed_files=["fix.py", "test_fix.py"], test_results=[_tr(True)])
    assert r.verdict.value == "approved"
