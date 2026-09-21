"""Preflight: cheap OpenCode connectivity/model check BEFORE expensive work.

Runs before cloning large repos or spending agent tokens. Results are cached in
the ModelHealthStore so a job does not re-probe every configured model.
Distinguishes BLOCKED_PROVIDER (inference impossible) from implementation failure.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field

from contributor.config import Settings
from contributor.opencode.client import ModelSpec, resolve_spec
from contributor.opencode.runner import OpenCodeRunner, classify_error
from contributor.persistence.model_health import ModelHealthStore

# Canonical tiers to validate, cheapest first.
PREFLIGHT_TIERS = ("fast", "cheap", "strong", "max")


@dataclass
class PreflightResult:
    ok: bool
    checks: dict[str, str] = field(default_factory=dict)  # name -> "OK" | reason
    spec: ModelSpec | None = None
    detail: str = ""
    reused: bool = False
    latency_s: float = 0.0
    error_kind: str = ""

    def summary(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.checks.items())


def _version_tuple(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v)
    return tuple(int(n) for n in nums[:3])


def check_binary(settings: Settings) -> tuple[bool, str]:
    path = shutil.which(settings.opencode_binary)
    if not path:
        return False, f"binary not found: {settings.opencode_binary}"
    try:
        p = subprocess.run(
            [settings.opencode_binary, "--version"],
            capture_output=True, text=True, timeout=30,
        )
        ver = (p.stdout or p.stderr or "").strip().split()[0]
        if _version_tuple(ver) < _version_tuple(settings.opencode_min_version):
            return False, f"version {ver} < minimum {settings.opencode_min_version}"
        return True, f"OK ({path}, v{ver})"
    except Exception as e:
        return False, f"version check failed: {e}"


def check_model_listed(settings: Settings, spec: ModelSpec) -> tuple[bool, str]:
    """Verify the selected provider/model appears in `opencode models`."""
    try:
        p = subprocess.run(
            [settings.opencode_binary, "models"],
            capture_output=True, text=True, timeout=60,
        )
        if p.returncode != 0:
            return False, f"models list failed: {(p.stderr or '')[:200]}"
        lines = [l.strip() for l in (p.stdout or "").splitlines() if l.strip()]
        if spec.model in lines or any(l.endswith("/" + spec.model.split("/")[-1]) for l in lines):
            return True, "OK (model listed)"
        return False, f"model {spec.model} not in `opencode models` output"
    except Exception as e:
        return False, f"models check failed: {e}"


def probe_inference(
    runner: OpenCodeRunner, spec: ModelSpec, *, timeout: int = 180
) -> tuple[bool, float, str, str]:
    """Tiny inference. Returns (ok, latency_s, error_kind, raw_detail)."""
    res = runner.run_with_prompt(
        "Reply with exactly OK", workdir=".", model=spec.model, variant=spec.variant, timeout=timeout
    )
    ok = res.exit_code == 0 and "OK" in (res.stdout or "").upper()
    kind = "" if ok else (res.error_kind or classify_error(res.exit_code, res.stdout, res.stderr))
    detail = "" if ok else (res.stderr or res.stdout or "")[:300]
    return ok, getattr(res, "duration_s", 0.0), kind, detail


def run_preflight(
    settings: Settings,
    runner: OpenCodeRunner | None = None,
    *,
    tier: str = "strong",
    probe_timeout: int | None = None,
    health_store: ModelHealthStore | None = None,
) -> PreflightResult:
    """Binary -> version -> cached health -> model listed -> tiny inference."""
    probe_timeout = probe_timeout or settings.model_preflight_timeout_s
    runner = runner or OpenCodeRunner(settings)
    spec = resolve_spec(settings, tier)
    checks: dict[str, str] = {}
    ok, msg = check_binary(settings)
    checks["binary"] = msg
    if not ok:
        return PreflightResult(False, checks, spec, msg, error_kind="OPENCODE_BINARY_NOT_FOUND")

    # Reuse recent health instead of spending another inference. Unavailable
    # models are also reused within TTL so blocked/rate-limited models are not
    # retried repeatedly.
    if health_store is not None:
        cached = health_store.get(spec)
        if cached is not None:
            if cached.is_ok:
                checks["inference"] = f"OK (cached, {cached.latency_s:.1f}s)"
                return PreflightResult(True, checks, spec, "preflight OK (cached)", reused=True,
                                       latency_s=cached.latency_s)
            checks["inference"] = f"unavailable (cached: {cached.error_class})"
            return PreflightResult(False, checks, spec,
                                   f"model unavailable (cached: {cached.error_class})",
                                   reused=True, error_kind=cached.error_class)

    ok, msg = check_model_listed(settings, spec)
    checks["model"] = msg
    if not ok:
        if health_store is not None:
            health_store.put(spec, ok=False, error_class="MODEL_NOT_FOUND")
        return PreflightResult(False, checks, spec, msg, error_kind="MODEL_NOT_FOUND")

    ok, latency, kind, detail = probe_inference(runner, spec, timeout=probe_timeout)
    if not ok:
        if health_store is not None:
            health_store.put(spec, ok=False, error_class=kind)
        checks["inference"] = f"FAIL ({kind}): {detail[:160]}"
        return PreflightResult(False, checks, spec, f"inference probe failed ({kind}): {detail}",
                               error_kind=kind)
    if health_store is not None:
        health_store.put(spec, ok=True, latency_s=latency)
    checks["inference"] = f"OK ({latency:.1f}s)"
    return PreflightResult(True, checks, spec, "preflight OK", latency_s=latency)


def run_all_preflights(
    settings: Settings,
    runner: OpenCodeRunner | None = None,
    *,
    health_store: ModelHealthStore | None = None,
    probe_timeout: int | None = None,
) -> dict[str, PreflightResult]:
    """Preflight every canonical tier. Does not raise; reports per tier."""
    runner = runner or OpenCodeRunner(settings)
    out: dict[str, PreflightResult] = {}
    for tier in PREFLIGHT_TIERS:
        out[tier] = run_preflight(
            settings, runner, tier=tier, probe_timeout=probe_timeout, health_store=health_store
        )
    return out


def any_usable(results: dict[str, PreflightResult]) -> bool:
    return any(r.ok for r in results.values())
