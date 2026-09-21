"""Doctor: comprehensive, safe diagnostics for the OpenCode integration.

No secrets are ever printed. Validates every configured model tier and marks
individual models unavailable without failing the whole system unless no usable
model remains. Run via `contributor doctor [--json]`.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
from dataclasses import dataclass, field

from contributor.config import Settings
from contributor.opencode.client import resolve_spec
from contributor.opencode.preflight import (
    PREFLIGHT_TIERS,
    check_binary,
    check_model_listed,
    probe_inference,
)
from contributor.opencode.runner import OpenCodeRunner
from contributor.persistence.model_health import ModelHealthStore


@dataclass
class DoctorReport:
    sections: dict[str, dict[str, str]] = field(default_factory=dict)
    flags: dict[str, bool] = field(default_factory=dict)
    ok: bool = True
    usable_models: list[str] = field(default_factory=list)
    unavailable_models: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, section: str, name: str, status: str, ok: bool) -> None:
        self.sections.setdefault(section, {})[name] = status
        self.flags[f"{section}.{name}"] = ok
        if not ok:
            self.warnings.append(f"{section}.{name}: {status}")

    def is_ok(self, section: str, name: str) -> bool:
        return self.flags.get(f"{section}.{name}", True)


def check_auth(settings: Settings) -> tuple[bool, str]:
    try:
        p = subprocess.run(
            [settings.opencode_binary, "auth", "list"],
            capture_output=True, text=True, timeout=30,
        )
        out = (p.stdout or "") + (p.stderr or "")
        if "●" in out or "api" in out.lower():
            return True, "OK (credentials configured)"
        if p.returncode == 0:
            return False, "no credentials listed"
        return False, f"auth list failed: {out[:200]}"
    except Exception as e:
        return False, f"auth check failed: {e}"


def check_network(host: str = "opencode.ai", port: int = 443, timeout: float = 10.0) -> tuple[bool, str]:
    try:
        ip = socket.gethostbyname(host)
    except Exception as e:
        return False, f"DNS failed for {host}: {e}"
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        return True, f"OK (DNS={ip}, TCP {port} open)"
    except Exception as e:
        return False, f"TCP {host}:{port} failed: {e}"


def check_docker(image: str = "contributor-sandbox:latest") -> tuple[bool, str]:
    if not shutil.which("docker"):
        return False, "docker CLI not on PATH (local fallback will be used)"
    try:
        p = subprocess.run(["docker", "image", "inspect", image],
                           capture_output=True, text=True, timeout=60)
        err = (p.stderr or "").lower()
        if p.returncode == 0:
            return True, "OK (sandbox image present)"
        if "permission denied" in err or "got permission" in err or "cannot connect" in err:
            return False, "docker socket permission denied (add user to docker group)"
        return False, "docker OK but sandbox image missing (build it)"
    except Exception as e:
        return False, f"docker check failed: {e}"


def run_doctor(
    settings: Settings,
    *,
    probe_inference_enabled: bool = True,
    health_store: ModelHealthStore | None = None,
) -> DoctorReport:
    rep = DoctorReport()
    runner = OpenCodeRunner(settings)

    binary_ok, msg = check_binary(settings)
    rep.add("OpenCode", "binary", msg, binary_ok)
    try:
        p = subprocess.run([settings.opencode_binary, "--version"], capture_output=True, text=True, timeout=30)
        ver = (p.stdout or p.stderr or "").strip().split()[0][:20] if (p.stdout or p.stderr) else "unknown"
        rep.add("OpenCode", "version", f"OK (v{ver})" if p.returncode == 0 else ver, p.returncode == 0)
    except Exception as e:
        rep.add("OpenCode", "version", str(e)[:100], False)

    auth_ok, msg = check_auth(settings)
    rep.add("Authentication", "credentials", msg, auth_ok)

    any_usable = False
    for tier in PREFLIGHT_TIERS:
        spec = resolve_spec(settings, tier)
        section = tier.upper()
        rep.add(section, "model", spec.model or "(unset)", bool(spec.model))
        rep.add(section, "variant", spec.variant or "(default)", True)
        rep.add(section, "auth", msg, auth_ok)
        if not binary_ok or not spec.model:
            rep.add(section, "inference", "skipped (no binary/model)", False)
            rep.unavailable_models.append(spec.key())
            continue
        if not probe_inference_enabled:
            listed_ok, listed_msg = check_model_listed(settings, spec)
            rep.add(section, "model_listed", listed_msg, listed_ok)
            if listed_ok:
                any_usable = True
                rep.usable_models.append(spec.key())
            else:
                rep.unavailable_models.append(spec.key())
            continue
        # Reuse cached health when available.
        if health_store is not None:
            cached = health_store.get(spec)
            if cached is not None and cached.is_ok:
                rep.add(section, "inference", f"OK (cached, {cached.latency_s:.1f}s)", True)
                rep.add(section, "latency", f"{cached.latency_s:.1f}s", True)
                any_usable = True
                rep.usable_models.append(spec.key())
                continue
        ok, latency, kind, detail = probe_inference(runner, spec, timeout=settings.model_preflight_timeout_s)
        if ok:
            rep.add(section, "inference", "OK", True)
            rep.add(section, "latency", f"{latency:.1f}s", True)
            rep.usable_models.append(spec.key())
            any_usable = True
            if health_store is not None:
                health_store.put(spec, ok=True, latency_s=latency)
        else:
            rep.add(section, "inference", f"FAIL ({kind})", False)
            rep.unavailable_models.append(spec.key())
            if health_store is not None:
                health_store.put(spec, ok=False, error_class=kind)

    net_ok, msg = check_network()
    rep.add("Network", "endpoint", msg, net_ok)

    docker_ok, msg = check_docker(settings.sandbox_image)
    rep.add("Sandbox", "docker", msg, docker_ok)

    # System is usable as long as the provider can infer with >=1 model.
    rep.ok = binary_ok and auth_ok and any_usable
    return rep
