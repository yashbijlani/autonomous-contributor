"""Unit: model routing tiers + escalation."""
from contributor.agents.routing import escalate_model, model_for_difficulty, tier_for_difficulty
from contributor.config import Settings


def test_tiers():
    assert tier_for_difficulty(1) == "high"
    assert tier_for_difficulty(2) == "high"
    assert tier_for_difficulty(3) == "high"
    assert tier_for_difficulty(4) == "xhigh"
    assert tier_for_difficulty(5) == "xhigh"


def test_model_names_configurable():
    s = Settings(opencode_model_standard="std-model", opencode_model_xhigh="big-model")
    assert model_for_difficulty(s, 2) == "std-model"
    assert model_for_difficulty(s, 5) == "big-model"


def test_escalation_high_to_xhigh():
    s = Settings(opencode_model_standard="std", opencode_model_xhigh="big")
    assert escalate_model(s, "std") == "big"
    assert escalate_model(s, "high") == "big"


def test_escalation_xhigh_to_none():
    s = Settings(opencode_model_standard="std", opencode_model_xhigh="big")
    assert escalate_model(s, "big") is None
