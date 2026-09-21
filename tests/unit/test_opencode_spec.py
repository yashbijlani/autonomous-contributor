"""Regression: tier aliases must never reach --model; variants go via --variant."""
from contributor.config import Settings
from contributor.opencode.client import (
    ModelSpec,
    OpenCodeRequest,
    build_argv,
    resolve_spec,
    split_model_id,
)


def _s(**kw):
    base = dict(
        opencode_model_standard="opencode-go/muse-spark-1.3-contributor",
        opencode_model_xhigh="opencode-go/muse-spark-1.3-contributor",
        opencode_variant_standard="high",
        opencode_variant_xhigh="xhigh",
    )
    base.update(kw)
    return Settings(**base)


def test_resolve_spec_splits_model_and_variant():
    spec = resolve_spec(_s(), "high")
    assert spec.model == "opencode-go/muse-spark-1.3-contributor"
    assert spec.variant == "high"
    assert spec.provider == "opencode-go"
    spec2 = resolve_spec(_s(), "xhigh")
    assert spec2.variant == "xhigh"


def test_bare_alias_never_becomes_model_id():
    # Unknown bare aliases fall back to the standard model, never passed through.
    spec = resolve_spec(_s(), "high")
    assert "/" in spec.model
    argv = build_argv(_s(), OpenCodeRequest(prompt="hi", model=spec.model, variant=spec.variant))
    assert "--model" in argv and "high" not in argv[argv.index("--model") + 1 : argv.index("--model") + 2]
    assert argv[argv.index("--model") + 1] == "opencode-go/muse-spark-1.3-contributor"
    assert "--variant" in argv
    assert argv[argv.index("--variant") + 1] == "high"


def test_build_argv_variant_xhigh():
    s = _s()
    spec = resolve_spec(s, "xhigh")
    argv = build_argv(s, OpenCodeRequest(prompt="hi", model=spec.model, variant=spec.variant))
    assert argv[argv.index("--variant") + 1] == "xhigh"


def test_full_id_passthrough():
    spec = resolve_spec(_s(), "other-provider/some-model")
    assert spec.model == "other-provider/some-model"
    assert spec.variant == ""


def test_split_model_id():
    assert split_model_id("a/b") == ("a", "b")
    assert split_model_id("bare") == ("", "bare")


def test_label_redacts_nothing_but_readable():
    spec = ModelSpec(provider="p", model="p/m", variant="high")
    assert "p/m" in spec.label() and "high" in spec.label()


def _tiers(**kw):
    base = dict(
        opencode_model_fast="p/fast", opencode_variant_fast="",
        opencode_model_cheap="p/cheap", opencode_variant_cheap="",
        opencode_model_strong="p/strong", opencode_variant_strong="high",
        opencode_model_max="p/max", opencode_variant_max="xhigh",
    )
    base.update(kw)
    return Settings(**base)


def test_canonical_tiers_resolve():
    s = _tiers()
    assert resolve_spec(s, "fast").model == "p/fast"
    assert resolve_spec(s, "cheap").model == "p/cheap"
    assert resolve_spec(s, "strong").model == "p/strong"
    assert resolve_spec(s, "strong").variant == "high"
    assert resolve_spec(s, "max").model == "p/max"
    assert resolve_spec(s, "max").variant == "xhigh"


def test_tier_alias_never_reaches_model_arg():
    s = _tiers()
    for tier in ("fast", "cheap", "strong", "max", "high", "xhigh"):
        spec = resolve_spec(s, tier)
        argv = build_argv(s, OpenCodeRequest(prompt="hi", model=spec.model, variant=spec.variant))
        model_arg = argv[argv.index("--model") + 1]
        assert "/" in model_arg, f"tier {tier} leaked bare alias into --model: {model_arg}"
        assert model_arg != tier


def test_tier_fallback_when_unset():
    s = Settings(opencode_model_strong="", opencode_model_standard="p/std", opencode_model_max="")
    assert resolve_spec(s, "strong").model == "p/std"
    assert resolve_spec(s, "max").model  # falls back to xhigh default
