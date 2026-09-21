"""Unit: ModelRouter tier selection, escalation, and policy override."""
from contributor.agents.model_router import (
    TASK_DEBUG,
    TASK_ENV_DISCOVERY,
    TASK_IMPLEMENT,
    TASK_PLAN,
    TASK_REVIEW,
    TASK_TRIAGE,
    ModelRouter,
    next_tier,
)
from contributor.config import Settings


def _s(**kw):
    base = dict(
        opencode_model_fast="p/fast",
        opencode_model_cheap="p/cheap",
        opencode_model_strong="p/strong",
        opencode_model_max="p/max",
        opencode_variant_fast="",
        opencode_variant_cheap="",
        opencode_variant_strong="",
        opencode_variant_max="",
    )
    base.update(kw)
    return Settings(**base)


def test_task_baseline_tiers():
    r = ModelRouter(_s())
    assert r.select(TASK_TRIAGE).model == "p/cheap"
    assert r.select(TASK_ENV_DISCOVERY).model == "p/cheap"
    assert r.select(TASK_PLAN).model == "p/cheap"
    assert r.select(TASK_IMPLEMENT).model == "p/strong"
    assert r.select(TASK_DEBUG).model == "p/strong"
    assert r.select(TASK_REVIEW).model == "p/strong"


def test_difficulty_bumps():
    r = ModelRouter(_s())
    assert r.select(TASK_PLAN, difficulty=4).model == "p/strong"
    assert r.select(TASK_IMPLEMENT, difficulty=5).model == "p/max"
    assert r.select(TASK_IMPLEMENT, difficulty=4).model == "p/strong"


def test_failure_escalation():
    r = ModelRouter(_s())
    assert r.select(TASK_IMPLEMENT, difficulty=3, previous_failures=0).model == "p/strong"
    assert r.select(TASK_IMPLEMENT, difficulty=3, previous_failures=1).model == "p/max"
    assert r.select(TASK_DEBUG, difficulty=3, previous_failures=2).model == "p/max"


def test_never_beyond_max():
    r = ModelRouter(_s())
    assert r.select(TASK_IMPLEMENT, difficulty=5, previous_failures=9).model == "p/max"


def test_next_tier_chain():
    assert next_tier("fast") == "cheap"
    assert next_tier("cheap") == "strong"
    assert next_tier("strong") == "max"
    assert next_tier("max") is None


def test_spec_never_contains_bare_alias():
    r = ModelRouter(_s())
    for task in (TASK_TRIAGE, TASK_PLAN, TASK_IMPLEMENT, TASK_DEBUG, TASK_REVIEW):
        spec = r.select(task, difficulty=3, previous_failures=0)
        assert "/" in spec.model, f"{task} produced a bare alias model: {spec.model}"


def test_policy_override():
    r = ModelRouter(_s(model_router_policy='{"implement": "max"}'))
    assert r.select(TASK_IMPLEMENT).model == "p/max"


def test_malformed_policy_falls_back():
    r = ModelRouter(_s(model_router_policy="{not json"))
    assert r.select(TASK_IMPLEMENT).model == "p/strong"


def test_variant_preserved_separately():
    r = ModelRouter(_s(opencode_model_strong="p/strong", opencode_variant_strong="high"))
    spec = r.select(TASK_IMPLEMENT)
    assert spec.model == "p/strong"
    assert spec.variant == "high"


def test_select_available_skips_unavailable_preferred():
    r = ModelRouter(_s())
    unavailable = {"p/strong", "p/max"}

    def is_available(spec):
        return spec.model not in unavailable

    tier, spec = r.select_available(TASK_IMPLEMENT, 3, 0, is_available)
    assert spec.model == "p/cheap"  # fell back past strong/max
    assert tier in ("fast", "cheap")


def test_select_available_prefers_preferred_when_healthy():
    r = ModelRouter(_s())
    tier, spec = r.select_available(TASK_IMPLEMENT, 3, 0, lambda s: True)
    assert spec.model == "p/strong" and tier == "strong"


def test_select_available_falls_back_when_all_unavailable():
    r = ModelRouter(_s())
    tier, spec = r.select_available(TASK_IMPLEMENT, 3, 0, lambda s: False)
    assert spec.model == "p/strong"  # preferred still returned
