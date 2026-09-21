"""Debugger: bounded repair loop. Evidence-rich prompt + ModelRouter escalation."""
from __future__ import annotations

from pathlib import Path

from contributor.agents.implementer import _available, difficulty_of
from contributor.agents.model_router import TASK_DEBUG, ModelRouter
from contributor.agents.result import AgentRunResult
from contributor.config import Settings
from contributor.models.state import JobState, TestResult
from contributor.opencode.prompts import build_debug_prompt
from contributor.opencode.runner import OpenCodeRunner


def failure_evidence(state: JobState) -> str:
    parts: list[str] = []
    if state.test_results:
        t: TestResult = state.test_results[-1]
        parts.append(f"FAILING TEST command={t.command} exit={t.exit_code} eco={t.ecosystem}")
        parts.append(f"stdout:\n{t.stdout[-4000:]}")
        parts.append(f"stderr:\n{t.stderr[-4000:]}")
    if state.review_result and state.review_result.issues:
        parts.append("REVIEWER FINDINGS: " + state.review_result.summary)
        for i in state.review_result.issues[:10]:
            parts.append(f"- [{i.severity}] {i.file}: {i.message}")
    if state.errors:
        parts.append("ERRORS: " + " | ".join(state.errors[-5:]))
    return "\n".join(parts)


def run_debug(
    state: JobState,
    settings: Settings,
    runner: OpenCodeRunner | None = None,
    health_store=None,
) -> AgentRunResult:
    runner = runner or OpenCodeRunner(settings)
    router = ModelRouter(settings)
    # Repeated failures escalate the tier (strong -> max).
    previous_failures = max(0, state.debug_attempt - 1) + max(0, state.implementation_attempt - 1)
    if health_store is not None:
        tier, spec = router.select_available(
            TASK_DEBUG, difficulty_of(state), previous_failures, _available(health_store)
        )
    else:
        tier = router.select_tier(TASK_DEBUG, difficulty_of(state), previous_failures)
        spec = router.select(TASK_DEBUG, difficulty_of(state), previous_failures)
    prompt = build_debug_prompt(state, failing=failure_evidence(state))
    res = runner.run_with_prompt(
        prompt, workdir=Path(state.workspace_path), model=spec.model, variant=spec.variant
    )
    state.model_selections["tier"] = tier
    state.model_selections["debug"] = spec.label()
    return AgentRunResult(
        code=res.exit_code,
        stdout=res.stdout,
        stderr=res.stderr,
        error_kind=getattr(res, "error_kind", ""),
        model=spec.model,
        variant=spec.variant,
        tier=tier,
        duration_s=getattr(res, "duration_s", 0.0),
    )
