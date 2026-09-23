"""Unit: transparent selection filters."""
from contributor.agents.triage import assess_solvability, triage_issue
from contributor.benchmark.select import (
    SelectionThresholds,
    is_selectable,
    rejection_reasons,
    selection_reasons,
)

GOOD = """
The parser crashes with a traceback.

Steps to reproduce:
1. run the parser
Expected behavior: it should return a result.
Actual: it raises in src/parser.py.
"""


def _pair(title, body, labels=None):
    return assess_solvability(title, body, labels=labels), triage_issue(title, body, labels=labels or [])


def test_good_candidate_is_selectable():
    a, tr = _pair("Crash in parser", GOOD, ["bug"])
    assert is_selectable(a, tr, ["bug"], SelectionThresholds())
    assert rejection_reasons(a, tr, ["bug"], SelectionThresholds()) == []


def test_avoid_label_rejects():
    a, tr = _pair("Crash in parser", GOOD, ["bug", "needs-mre"])
    reasons = rejection_reasons(a, tr, ["bug", "needs-mre"], SelectionThresholds())
    assert any(r.startswith("avoid_label:") for r in reasons)


def test_complexity_threshold_rejects():
    a, tr = _pair(
        "Refactor architecture",
        "This is a redesign across multiple modules with a migration. " * 5,
    )
    reasons = rejection_reasons(a, tr, [], SelectionThresholds(max_complexity=2))
    assert any(r.startswith("complexity>") for r in reasons)


def test_maintainer_decision_rejects():
    a, tr = _pair(
        "Breaking change proposal",
        "We need a product decision on which approach should we take for this breaking change. " * 4,
    )
    reasons = rejection_reasons(a, tr, [], SelectionThresholds())
    assert "requires_maintainer_decision" in reasons


def test_external_service_rejects():
    a, tr = _pair(
        "Private registry support",
        "Talk to the external service and authenticate with an api key to fetch packages. " * 4,
    )
    reasons = rejection_reasons(a, tr, [], SelectionThresholds())
    assert "requires_external_service" in reasons


def test_human_required_triage_rejects():
    a, tr = _pair(
        "Which approach should we take?",
        "We need a design decision and product decision before doing anything. " * 4,
    )
    reasons = rejection_reasons(a, tr, [], SelectionThresholds())
    assert any(r == "triage:human_required" for r in reasons)


def test_selection_reasons_are_factual():
    a, _ = _pair("Crash in parser", GOOD, ["bug"])
    reasons = selection_reasons(a, SelectionThresholds())
    assert any(r.startswith("complexity=") for r in reasons)
    assert "actionable" in reasons
    assert "no_maintainer_decision" in reasons
