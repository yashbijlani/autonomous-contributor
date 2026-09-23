"""CI repair agent: bounded, evidence-rich repair of a CI failure.

Runs in the SAME repository/sandbox as the contribution. The orchestrator owns
the bound (``max_ci_repair_cycles``); the agent never decides how many repair
iterations exist.
"""
from __future__ import annotations

from pathlib import Path

from contributor.agents.debugger import failure_evidence
from contributor.agents.implementer import _available, difficulty_of
from contributor.agents.model_router import TASK_DEBUG, ModelRouter
from contributor.agents.result import AgentRunResult
from contributor.config import Settings
from contributor.models.state import JobState
from contributor.opencode.prompts import build_ci_repair_prompt
from contributor.opencode.runner import OpenCodeRunner


def run_ci_repair(
    state: JobState,
    settings: Settings,
    runner: OpenCodeRunner | None = None,
    health_store=None,
) -> AgentRunResult:
    runner = runner or OpenCodeRunner(settings)
    router = ModelRouter(settings)
    # Repeated CI repair attempts escalate the tier like repeated debug failures.
    previous_failures = max(0, state.ci_repair_attempt - 1) + max(0, state.debug_attempt)
    if health_store is not None:
        tier, spec = router.select_available(
            TASK_DEBUG, difficulty_of(state), previous_failures, _available(health_store)
        )
    else:
        tier = router.select_tier(TASK_DEBUG, difficulty_of(state), previous_failures)
        spec = router.select(TASK_DEBUG, difficulty_of(state), previous_failures)
    prompt = build_ci_repair_prompt(state, failing=failure_evidence(state))
    res = runner.run_with_prompt(
        prompt, workdir=Path(state.workspace_path), model=spec.model, variant=spec.variant
    )
    state.model_selections["ci_repair_tier"] = tier
    state.model_selections["ci_repair"] = spec.label()
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
