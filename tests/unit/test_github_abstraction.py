"""Unit: GitHub abstraction — CI interpretation, PR body, discovery query."""
from contributor.config import Settings
from contributor.discovery.github_search import DiscoveryFilters, build_query
from contributor.github.ci import interpret_ci
from contributor.github.pull_requests import render_pr_body
from contributor.models.state import JobState


def test_interpret_ci_pass():
    ci = interpret_ci({"combined": {"state": "success", "statuses": []}, "check_runs": [
        {"name": "t", "status": "completed", "conclusion": "success"}]})
    assert ci.state == "pass"


def test_interpret_ci_fail():
    ci = interpret_ci({"combined": {"state": "failure", "statuses": [{"context": "c", "state": "failure"}]}, "check_runs": []})
    assert ci.state == "fail"


def test_interpret_ci_pending():
    ci = interpret_ci({"combined": {"state": "pending", "statuses": []}, "check_runs": [
        {"name": "t", "status": "in_progress", "conclusion": ""}]})
    assert ci.state == "pending"


def test_interpret_ci_unknown_empty():
    ci = interpret_ci({"combined": {}, "check_runs": []})
    assert ci.state == "unknown"


def test_render_pr_body_contains_sections():
    s = JobState(job_id="abc", repository="o/r", issue_number=1, issue_title="T")
    body = render_pr_body(s, Settings())
    assert "Fixes #1" in body
    assert "AI assistance" in body or "AI-generated" in body or "AI" in body


def test_build_query():
    q = build_query(DiscoveryFilters(language="python", label=["good first issue"], repo="o/r", min_stars=10, keyword="bug"))
    assert "language:python" in q
    assert "repo:o/r" in q
    assert "stars:>=10" in q
