"""Model routing: difficulty -> tier, escalation on repeated failure.

Tiers ("high"/"xhigh") are reasoning-effort levels resolved to concrete
(provider/model, variant) specs via opencode.client.resolve_spec. Tier names
must NEVER be passed as --model values (opencode requires provider/model).
"""
from __future__ import annotations

from contributor.config import Settings
from contributor.opencode.client import ModelSpec, resolve_model, resolve_spec


def tier_for_difficulty(difficulty: int) -> str:
    if difficulty >= 4:
        return "xhigh"
    return "high"


def spec_for_difficulty(settings: Settings, difficulty: int) -> ModelSpec:
    return resolve_spec(settings, tier_for_difficulty(difficulty))


def model_for_difficulty(settings: Settings, difficulty: int) -> str:
    """Back-compat: full provider/model ID for a difficulty (no bare aliases)."""
    return resolve_model(settings, tier_for_difficulty(difficulty))


def escalate_tier(tier: str) -> str | None:
    """high -> xhigh; xhigh (or unknown) -> None (human escalation)."""
    if tier == "high":
        return "xhigh"
    return None


def escalate_model(settings: Settings, current_model: str) -> str | None:
    """Back-compat tier escalation given a resolved model ID.

    Returns the xhigh model ID when currently on the standard model, else None.
    """
    std = resolve_model(settings, "high")
    xhi = resolve_model(settings, "xhigh")
    if current_model in ("", std, "high") and xhi and xhi != current_model:
        return xhi
    if current_model == "high" and xhi:
        return xhi
    return None
