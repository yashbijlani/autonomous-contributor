"""Cheap-model triage: an LLM pass over candidate issue bodies.

The deterministic assessor in `agents.triage` is a fast pre-filter; this pass
inspects the full body with the CHEAP tier (per ModelRouter) and returns the
same explicit factual dimensions plus an accept/reject/human_required decision.

Never trusts the model for safety: the programmatic blocklist in
`agents.triage.triage_issue` still runs first and cannot be overridden here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from contributor.agents.model_router import TASK_TRIAGE, ModelRouter
from contributor.agents.triage import assess_solvability
from contributor.config import Settings
from contributor.models.state import TriageAssessment
from contributor.opencode.runner import OpenCodeRunner

_PROMPT = """You are a strict triage classifier for an autonomous coding agent.
The agent must turn a real GitHub issue into a small, correct, tested code change
inside a Docker sandbox (no special hardware, no external services, no host OS).

Inspect the issue text below and classify it. Output ONLY a single JSON object
(no prose, no markdown fences) with EXACTLY these keys:

{{
  "actionable": true|false,
  "reproducible": true|false,
  "clear_expected_behavior": true|false,
  "likely_code_change": true|false,
  "likely_test_change": true|false,
  "container_testable": true|false,
  "requires_maintainer_decision": true|false,
  "requires_external_service": true|false,
  "estimated_complexity": 1-5,
  "confidence": 0.0-1.0,
  "decision": "accept" | "reject" | "human_required",
  "reason": "one short sentence"
}}

Reject (decision="reject") when the issue is: a security vulnerability, a
question/support request, too vague/underspecified, missing a reproduction, a
large architectural proposal, a new feature that needs design decisions, a
refactor/removal reminder, or otherwise not solvable as a small tested change.
Use decision="human_required" when a maintainer/product decision is required.
Only use decision="accept" for concrete, reproducible bugs or clearly specified
small enhancements with an obvious acceptance condition.

Title: {title}
Labels: {labels}
Body:
{body}
"""


@dataclass
class LLMTriageResult:
    assessment: TriageAssessment
    decision: str = "reject"
    reason: str = ""
    model: str = ""
    variant: str = ""
    tier: str = ""
    raw: str = ""
    ok: bool = False
    error: str = ""
    signals: list[str] = field(default_factory=list)


def _extract_json(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except Exception:
        # tolerate trailing commas / minor noise
        try:
            return json.loads(candidate.replace(",}", "}").replace(",]", "]"))
        except Exception:
            return None


def _to_bool(v: object, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return default


def _to_int(v: object, default: int = 3) -> int:
    try:
        return max(1, min(5, int(v)))  # type: ignore[arg-type]
    except Exception:
        return default


def _to_float(v: object, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(v)))  # type: ignore[arg-type]
    except Exception:
        return default


def llm_assess(
    settings: Settings,
    runner: OpenCodeRunner,
    *,
    title: str,
    body: str,
    labels: list[str] | None = None,
    workdir: str = ".",
    timeout: int | None = None,
) -> LLMTriageResult:
    """Run the CHEAP tier over one issue. Falls back to deterministic on failure."""
    fallback = assess_solvability(title, body, labels=labels)
    router = ModelRouter(settings)
    tier = router.select_tier(TASK_TRIAGE, difficulty=2, previous_failures=0)
    spec = router.select(TASK_TRIAGE, difficulty=2, previous_failures=0)
    prompt = _PROMPT.format(
        title=title,
        labels=", ".join(labels or []) or "(none)",
        body=(body or "")[:12000],
    )
    res = runner.run_with_prompt(
        prompt, workdir=workdir, model=spec.model, variant=spec.variant, timeout=timeout
    )
    if res.exit_code != 0:
        return LLMTriageResult(
            assessment=fallback, model=spec.model, variant=spec.variant, tier=tier,
            ok=False, error=res.error_kind or f"exit_{res.exit_code}",
        )
    data = _extract_json(res.stdout or "")
    if not data:
        return LLMTriageResult(
            assessment=fallback, model=spec.model, variant=spec.variant, tier=tier,
            ok=False, error="unparseable_json", raw=(res.stdout or "")[-500:],
        )
    decision = str(data.get("decision", "reject")).strip().lower()
    if decision not in ("accept", "reject", "human_required"):
        decision = "reject"
    assessment = TriageAssessment(
        actionable=_to_bool(data.get("actionable")),
        reproducible=_to_bool(data.get("reproducible")),
        clear_expected_behavior=_to_bool(data.get("clear_expected_behavior")),
        likely_code_change=_to_bool(data.get("likely_code_change")),
        likely_test_change=_to_bool(data.get("likely_test_change")),
        container_testable=_to_bool(data.get("container_testable")),
        requires_maintainer_decision=_to_bool(data.get("requires_maintainer_decision")),
        requires_external_service=_to_bool(data.get("requires_external_service")),
        estimated_complexity=_to_int(data.get("estimated_complexity")),
        confidence=_to_float(data.get("confidence")),
        clear_reproduction=_to_bool(data.get("reproducible")),
        existing_relevant_tests=_to_bool(data.get("likely_test_change")),
        clear_location_in_code=_to_bool(data.get("likely_code_change")),
        obvious_acceptance_condition=_to_bool(data.get("clear_expected_behavior")),
        signals=["llm_triage"],
    )
    return LLMTriageResult(
        assessment=assessment,
        decision=decision,
        reason=str(data.get("reason", ""))[:300],
        model=spec.model,
        variant=spec.variant,
        tier=tier,
        raw=(res.stdout or "")[-500:],
        ok=True,
    )
