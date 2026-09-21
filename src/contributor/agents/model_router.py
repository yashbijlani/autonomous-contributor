"""ModelRouter: deterministic, configurable model selection by task complexity.

Tiers are reasoning/cost levels (FAST < CHEAP < STRONG < MAX). A tier resolves
to a concrete ModelSpec(provider, model, variant) via config; tier names are
never passed to opencode as --model.

The router is pure/deterministic so it can be unit-tested and audited. Policy is
overridable via MODEL_ROUTER_POLICY (JSON: {"implement": "strong", ...}).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from contributor.config import Settings
from contributor.opencode.client import ModelSpec, resolve_spec

# Ordered cheapest -> strongest.
TIER_ORDER = ("fast", "cheap", "strong", "max")

TASK_TRIAGE = "triage"
TASK_ENV_DISCOVERY = "environment_discovery"
TASK_PLAN = "plan"
TASK_IMPLEMENT = "implementation"
TASK_DEBUG = "debug"
TASK_REVIEW = "review"
ALL_TASKS = (TASK_TRIAGE, TASK_ENV_DISCOVERY, TASK_PLAN, TASK_IMPLEMENT, TASK_DEBUG, TASK_REVIEW)

# Default base tier per task (before difficulty/failure escalation).
DEFAULT_POLICY: dict[str, str] = {
    TASK_TRIAGE: "cheap",
    TASK_ENV_DISCOVERY: "cheap",
    TASK_PLAN: "cheap",
    TASK_IMPLEMENT: "strong",
    TASK_DEBUG: "strong",
    TASK_REVIEW: "strong",
}

# Accept shorthand keys in MODEL_ROUTER_POLICY JSON.
POLICY_KEY_ALIASES = {
    "triage": TASK_TRIAGE,
    "env": TASK_ENV_DISCOVERY,
    "environment": TASK_ENV_DISCOVERY,
    "environment_discovery": TASK_ENV_DISCOVERY,
    "plan": TASK_PLAN,
    "planning": TASK_PLAN,
    "implement": TASK_IMPLEMENT,
    "implementation": TASK_IMPLEMENT,
    "code": TASK_IMPLEMENT,
    "debug": TASK_DEBUG,
    "debugging": TASK_DEBUG,
    "review": TASK_REVIEW,
}


def next_tier(tier: str) -> str | None:
    """cheap -> strong -> max -> None (never escalate beyond MAX)."""
    t = (tier or "").lower()
    if t == "fast":
        return "cheap"
    if t == "cheap":
        return "strong"
    if t == "strong":
        return "max"
    return None


@dataclass
class ModelRouter:
    settings: Settings

    def __post_init__(self) -> None:
        self.policy = dict(DEFAULT_POLICY)
        raw = (self.settings.model_router_policy or "").strip()
        if raw:
            try:
                override = json.loads(raw)
                for k, v in override.items():
                    task = POLICY_KEY_ALIASES.get(str(k).lower(), k)
                    if task in ALL_TASKS and isinstance(v, str) and v.lower() in TIER_ORDER:
                        self.policy[task] = v.lower()
            except Exception:
                # Malformed policy must not crash jobs; keep defaults.
                pass

    def tier_for(self, task_type: str, difficulty: int = 3, previous_failures: int = 0) -> str:
        tier = self.policy.get(task_type, "strong")
        # Difficulty bump (complex work needs a stronger model).
        if difficulty >= 4 and TIER_ORDER.index(tier) < TIER_ORDER.index("strong"):
            tier = "strong"
        if difficulty >= 5 and task_type in (TASK_IMPLEMENT, TASK_DEBUG, TASK_REVIEW):
            tier = "max"
        # Repeated-failure escalation.
        if previous_failures >= 1 and task_type in (TASK_IMPLEMENT, TASK_DEBUG):
            tier = next_tier(tier) or "max"
        if previous_failures >= 2:
            tier = "max"
        return tier

    def select(self, task_type: str, difficulty: int = 3, previous_failures: int = 0) -> ModelSpec:
        return resolve_spec(self.settings, self.tier_for(task_type, difficulty, previous_failures))

    def select_tier(self, task_type: str, difficulty: int = 3, previous_failures: int = 0) -> str:
        return self.tier_for(task_type, difficulty, previous_failures)

    def select_available(
        self,
        task_type: str,
        difficulty: int,
        previous_failures: int,
        is_available,
    ) -> tuple[str, ModelSpec]:
        """Like select(), but skips tiers whose model is known-unavailable.

        Tries the preferred tier first, then strongest->weakest. Falls back to the
        preferred tier if nothing is known-healthy (so the job still tries).
        """
        preferred = self.tier_for(task_type, difficulty, previous_failures)
        order = [preferred] + [t for t in reversed(TIER_ORDER) if t != preferred]
        for tier in order:
            spec = resolve_spec(self.settings, tier)
            try:
                if is_available(spec):
                    return tier, spec
            except Exception:
                return tier, spec
        return preferred, resolve_spec(self.settings, preferred)
