"""Implementer: invokes OpenCode as the coding worker via the ModelRouter."""
from __future__ import annotations

from pathlib import Path

from contributor.agents.model_router import TASK_IMPLEMENT, ModelRouter
from contributor.agents.result import AgentRunResult
from contributor.config import Settings
from contributor.models.state import JobState
from contributor.observability.logging import get_logger
from contributor.opencode.prompts import build_implement_prompt
from contributor.opencode.runner import OpenCodeRunner

log = get_logger("contributor.agents.implementer")


def difficulty_of(state: JobState) -> int:
    return state.triage_result.difficulty if state.triage_result else 3


def _available(health_store):
    """Return a predicate: a spec is available unless cached as unavailable."""

    def check(spec) -> bool:
        cached = health_store.get(spec)
        return not (cached is not None and not cached.is_ok)

    return check


def run_implementation(
    state: JobState,
    settings: Settings,
    runner: OpenCodeRunner | None = None,
    health_store=None,
) -> AgentRunResult:
    """Run the coding worker. `state.workspace_path` must exist."""
    runner = runner or OpenCodeRunner(settings)
    router = ModelRouter(settings)
    previous_failures = max(0, state.implementation_attempt - 1)
    if health_store is not None:
        tier, spec = router.select_available(
            TASK_IMPLEMENT, difficulty_of(state), previous_failures, _available(health_store)
        )
    else:
        tier = router.select_tier(TASK_IMPLEMENT, difficulty_of(state), previous_failures)
        spec = router.select(TASK_IMPLEMENT, difficulty_of(state), previous_failures)
    prompt = build_implement_prompt(state)
    res = runner.run_with_prompt(
        prompt, workdir=Path(state.workspace_path), model=spec.model, variant=spec.variant
    )
    state.model_selections["tier"] = tier
    state.model_selections["implement"] = spec.label()
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
