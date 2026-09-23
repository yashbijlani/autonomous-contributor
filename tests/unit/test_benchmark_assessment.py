"""Unit: structured solvability assessment (factual dimensions, no score)."""
from contributor.agents.triage import assess_solvability


GOOD_BUG = """
When running `uv pip install foo` the resolver crashes with a traceback.

Steps to reproduce:
1. run `uv pip install foo`
2. observe the error

Expected behavior: it should resolve successfully.
Actual behavior: it raises an exception in src/uv/resolver.py.
"""


def test_good_bug_is_actionable_and_container_testable():
    a = assess_solvability("Crash in resolver", GOOD_BUG, labels=["bug"])
    assert a.actionable
    assert a.reproducible
    assert a.clear_reproduction
    assert a.clear_expected_behavior
    assert a.likely_code_change
    assert a.container_testable
    assert a.estimated_complexity <= 3
    assert a.clear_location_in_code
    assert a.confidence > 0.5


def test_needs_mre_label_blocks_actionable():
    a = assess_solvability("Something is wrong", GOOD_BUG, labels=["needs-mre"])
    assert not a.actionable


def test_question_label_blocks_actionable():
    a = assess_solvability("How do I use this?", GOOD_BUG, labels=["question"])
    assert not a.actionable


def test_host_only_requirement_not_container_testable():
    a = assess_solvability(
        "systemd unit not starting",
        "The systemd service fails; requires systemctl and sudo on the host. " * 3,
    )
    assert not a.container_testable
    assert "host_only" in a.signals


def test_external_service_flagged():
    a = assess_solvability(
        "Support private registry auth",
        "We need to talk to the external service and authenticate with an api key. " * 3,
    )
    assert a.requires_external_service
    assert "external_service" in a.signals


def test_maintainer_decision_flagged():
    a = assess_solvability(
        "Breaking API change proposal",
        "We need a product decision on which approach should we take for the breaking change. " * 3,
    )
    assert a.requires_maintainer_decision


def test_good_first_issue_label_caps_complexity():
    a = assess_solvability(
        "Small typo fix",
        "There is a typo in the docs and we should fix it. " * 5,
        labels=["good first issue"],
    )
    assert a.estimated_complexity <= 2


def test_architectural_issue_high_complexity():
    a = assess_solvability(
        "Refactor the resolver architecture",
        "This requires a redesign across multiple modules and a migration path. " * 5,
    )
    assert a.estimated_complexity >= 4


def test_short_body_not_actionable():
    a = assess_solvability("Bug", "broken")
    assert not a.actionable
    assert "body_too_short" in a.signals


def test_security_issue_requires_maintainer_decision():
    a = assess_solvability(
        "Git checkout marker follows a repository-controlled symlink",
        "uv follows a symlink and truncates a file outside the checkout. "
        "This is a security vulnerability. " * 3,
    )
    assert a.requires_maintainer_decision
    assert "security" in a.signals
