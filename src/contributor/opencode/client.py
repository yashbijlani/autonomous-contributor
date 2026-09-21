"""OpenCode CLI client: builds argv, never shells out with secrets in args.

Model addressing (opencode >= 1.18):
  --model <provider>/<model>   full model ID, e.g. opencode-go/muse-spark-1.3-contributor
  --variant <name>             provider-specific reasoning effort, e.g. high, xhigh

Tier aliases ("high"/"xhigh") are NEVER valid --model values; they resolve to
(model, variant) pairs via resolve_spec().
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass

from contributor.config import Settings


@dataclass
class OpenCodeRequest:
    prompt: str
    model: str = ""
    variant: str = ""
    workdir: str = ""
    extra_args: list[str] | None = None


@dataclass(frozen=True)
class ModelSpec:
    """Provider + model + variant. Never collapse variant into the model ID.

    `variant` is optional ("" means the provider default).
    """
    provider: str = ""
    model: str = ""  # full provider/model ID, e.g. opencode-go/deepseek-v4.1-flash
    variant: str = ""  # e.g. high, xhigh (provider-specific); "" = default

    def label(self) -> str:
        base = self.model or "(default)"
        return f"{base} (variant={self.variant})" if self.variant else base

    def key(self) -> str:
        return f"{self.model}::{self.variant}"


# All accepted tier names -> (model attr, variant attr). New tiers are the
# canonical API; "high"/"xhigh" remain for backward compatibility.
TIER_FIELDS: dict[str, tuple[str, str]] = {
    "fast": ("opencode_model_fast", "opencode_variant_fast"),
    "cheap": ("opencode_model_cheap", "opencode_variant_cheap"),
    "strong": ("opencode_model_strong", "opencode_variant_strong"),
    "max": ("opencode_model_max", "opencode_variant_max"),
    "high": ("opencode_model_standard", "opencode_variant_standard"),
    "xhigh": ("opencode_model_xhigh", "opencode_variant_xhigh"),
}
TIER_ALIASES = frozenset(TIER_FIELDS)


def split_model_id(model_id: str) -> tuple[str, str]:
    """Split 'provider/model' into (provider, rest). Returns ('', id) if bare."""
    if "/" in model_id:
        provider, rest = model_id.split("/", 1)
        return provider, rest
    return "", model_id


def opencode_available(binary: str = "opencode") -> bool:
    return shutil.which(binary) is not None


def build_argv(settings: Settings, req: OpenCodeRequest) -> list[str]:
    argv = [settings.opencode_binary, "run"]
    if req.model:
        argv += ["--model", req.model]
    elif settings.opencode_default_model:
        argv += ["--model", settings.opencode_default_model]
    if req.variant:
        argv += ["--variant", req.variant]
    if settings.opencode_extra_args:
        argv += settings.opencode_extra_args.split()
    if req.extra_args:
        argv += req.extra_args
    argv.append(req.prompt)
    return argv


def _fallback_model(settings: Settings, tier: str) -> tuple[str, str]:
    """Standard/xhigh fallback when a tier's model is unset."""
    if tier == "max" or tier == "xhigh":
        return settings.opencode_model_xhigh, settings.opencode_variant_xhigh
    return settings.opencode_model_standard, settings.opencode_variant_standard


def resolve_spec(settings: Settings, tier: str) -> ModelSpec:
    """Resolve a tier to a concrete (model, variant).

    Accepts canonical tiers (fast/cheap/strong/max), legacy tiers
    (high/xhigh), or a full provider/model ID (variant="").
    """
    t = (tier or "").strip()
    if t in TIER_FIELDS:
        model_field, variant_field = TIER_FIELDS[t]
        model = getattr(settings, model_field, "") or ""
        variant = getattr(settings, variant_field, "") or ""
        if not model:
            model, variant = _fallback_model(settings, t)
    elif "/" in t:
        model, variant = t, ""
    else:
        # Unknown bare alias: do NOT pass it as --model (that fails server-side).
        model, variant = settings.opencode_model_standard, ""
    provider, _ = split_model_id(model)
    return ModelSpec(provider=provider, model=model, variant=variant)


def resolve_model(settings: Settings, tier: str) -> str:
    """Back-compat: return the full provider/model ID for a tier."""
    return resolve_spec(settings, tier).model


def resolve_variant(settings: Settings, tier: str) -> str:
    return resolve_spec(settings, tier).variant
