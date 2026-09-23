"""Transparent, configurable selection filters for benchmark candidates.

No vague "best issue" ranking: candidates are filtered on explicit factual
dimensions. Order of discovery is preserved (deterministic given the pool).
"""
from __future__ import annotations

from dataclasses import dataclass

from contributor.agents.triage import AVOID_LABELS
from contributor.models.state import TriageAssessment, TriageDecision, TriageResult


@dataclass
class SelectionThresholds:
    max_complexity: int = 3
    require_actionable: bool = True
    require_container_testable: bool = True
    forbid_maintainer_decision: bool = True
    forbid_external_service: bool = True
    min_confidence: float = 0.0


def label_rejections(labels: list[str] | None) -> list[str]:
    norm = {str(l).strip().lower() for l in (labels or [])}
    return sorted(norm & AVOID_LABELS)


def rejection_reasons(
    assessment: TriageAssessment,
    triage: TriageResult,
    labels: list[str] | None,
    thresholds: SelectionThresholds,
) -> list[str]:
    """Explicit reasons a candidate is not selectable (empty == selectable)."""
    reasons: list[str] = []
    if triage.decision != TriageDecision.ACCEPT:
        reasons.append(f"triage:{triage.decision.value}")
    if thresholds.require_actionable and not assessment.actionable:
        reasons.append("not_actionable")
    if thresholds.require_container_testable and not assessment.container_testable:
        reasons.append("not_container_testable")
    if thresholds.forbid_maintainer_decision and assessment.requires_maintainer_decision:
        reasons.append("requires_maintainer_decision")
    if thresholds.forbid_external_service and assessment.requires_external_service:
        reasons.append("requires_external_service")
    if assessment.estimated_complexity > thresholds.max_complexity:
        reasons.append(f"complexity>{thresholds.max_complexity}")
    if assessment.confidence < thresholds.min_confidence:
        reasons.append("low_confidence")
    reasons.extend(f"avoid_label:{l}" for l in label_rejections(labels))
    return reasons


def is_selectable(
    assessment: TriageAssessment,
    triage: TriageResult,
    labels: list[str] | None,
    thresholds: SelectionThresholds,
) -> bool:
    return not rejection_reasons(assessment, triage, labels, thresholds)


def selection_reasons(
    assessment: TriageAssessment,
    thresholds: SelectionThresholds,
) -> list[str]:
    """Positive, factual reasons a candidate passed the filters."""
    reasons = [f"complexity={assessment.estimated_complexity}"]
    if assessment.actionable:
        reasons.append("actionable")
    if assessment.container_testable:
        reasons.append("container_testable")
    if assessment.reproducible:
        reasons.append("reproducible")
    if assessment.clear_expected_behavior:
        reasons.append("clear_expected_behavior")
    if not assessment.requires_maintainer_decision:
        reasons.append("no_maintainer_decision")
    if not assessment.requires_external_service:
        reasons.append("no_external_service")
    return reasons
