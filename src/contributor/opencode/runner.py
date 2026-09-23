"""OpenCodeRunner: subprocess wrapper with timeout, exit-code, cancellation.

OpenCode runs with cwd=workspace so repository code stays inside the sandbox
checkout. Stdout/stderr are captured and truncated for event logging.
"""
from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from contributor.config import Settings
from contributor.observability.logging import get_logger
from contributor.opencode.client import OpenCodeRequest, build_argv, opencode_available

log = get_logger("contributor.opencode.runner")


# Normalized failure categories. Never collapse everything into "server error":
# the raw stderr is always preserved alongside.
ERROR_UNKNOWN = "UNKNOWN_PROVIDER_ERROR"
ERROR_BINARY_NOT_FOUND = "OPENCODE_BINARY_NOT_FOUND"
ERROR_AUTH = "AUTH_FAILURE"
ERROR_MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
ERROR_VARIANT_UNSUPPORTED = "VARIANT_UNSUPPORTED"
ERROR_PROVIDER_CONFIG = "PROVIDER_CONFIG_ERROR"
ERROR_PROVIDER_NETWORK = "PROVIDER_NETWORK_ERROR"
ERROR_PROVIDER_RATE_LIMIT = "PROVIDER_RATE_LIMIT"
ERROR_PROVIDER_SERVER = "PROVIDER_SERVER_ERROR"
ERROR_PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
ERROR_MALFORMED_REQUEST = "MALFORMED_REQUEST"
ERROR_SUBPROCESS = "SUBPROCESS_ERROR"
ERROR_PERMISSION_REJECTED = "PERMISSION_REJECTED"
ERROR_EMPTY_DIFF = "EMPTY_DIFF"

# Provider-level failures that retrying or debugging cannot resolve.
PROVIDER_BLOCKING_ERRORS = frozenset({
    ERROR_BINARY_NOT_FOUND,
    ERROR_AUTH,
    ERROR_MODEL_NOT_FOUND,
    ERROR_VARIANT_UNSUPPORTED,
    ERROR_PROVIDER_CONFIG,
    ERROR_PROVIDER_NETWORK,
    ERROR_PROVIDER_RATE_LIMIT,
    ERROR_PROVIDER_SERVER,
    ERROR_PROVIDER_TIMEOUT,
    ERROR_MALFORMED_REQUEST,
})


def detect_permission_rejection(stdout: str, stderr: str) -> bool:
    """opencode run auto-rejects out-of-project permissions in non-interactive
    mode and can still exit 0. Detect it so an empty implementation is not
    mistaken for success."""
    blob = f"{stdout}\n{stderr}".lower()
    return (
        "permission requested" in blob
        or "auto-rejecting" in blob
        or "rejected permission" in blob
        or "user rejected permission" in blob
    )


def classify_error(exit_code: int, stdout: str, stderr: str, *, timed_out: bool = False) -> str:
    """Map raw CLI output to a stable category. Secrets must never appear here;
    callers pass already-captured output (opencode never echoes credentials)."""
    blob = f"{stdout}\n{stderr}".lower()
    if timed_out or "timeout" in blob and exit_code == 124:
        return ERROR_PROVIDER_TIMEOUT
    if exit_code == 127 or "binary not found" in blob:
        return ERROR_BINARY_NOT_FOUND
    if any(k in blob for k in ("unauthorized", "401", "invalid api key", "api key", "authentication", "forbidden", "403")) and "model" not in blob:
        # auth signals — but 'model not found ... check api' style messages belong below
        if any(k in blob for k in ("unauthorized", "invalid api key", "authentication failed", "no auth", "not authenticated", "login required")):
            return ERROR_AUTH
    if any(k in blob for k in ("model not found", "unknown model", "model does not exist", "invalid model")):
        return ERROR_MODEL_NOT_FOUND
    if "variant" in blob and any(k in blob for k in ("unknown", "invalid", "unsupported", "not supported")):
        return ERROR_VARIANT_UNSUPPORTED
    if any(k in blob for k in ("rate limit", "429", "too many requests", "quota")):
        return ERROR_PROVIDER_RATE_LIMIT
    if any(k in blob for k in ("econnrefused", "enotfound", "econnreset", "network", "dns", "fetch failed", "socket hang up")):
        return ERROR_PROVIDER_NETWORK
    if any(k in blob for k in ("unexpected server error", "unknownerror", "500", "502", "503", "internal error", "overloaded")):
        return ERROR_PROVIDER_SERVER
    if any(k in blob for k in ("bad request", "400", "invalid request", "malformed", "validation")):
        return ERROR_MALFORMED_REQUEST
    if any(k in blob for k in ("no such provider", "provider not found", "missing provider", "endpoint", "baseurl", "base_url")):
        return ERROR_PROVIDER_CONFIG
    if exit_code != 0:
        return ERROR_SUBPROCESS
    return ""


@dataclass
class OpenCodeResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    cancelled: bool = False
    model: str = ""
    variant: str = ""
    error_kind: str = ""  # one of the ERROR_* categories, "" on success


class OpenCodeRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def reset(self) -> None:
        self._cancel.clear()

    @property
    def available(self) -> bool:
        return opencode_available(self.settings.opencode_binary)

    def run(self, req: OpenCodeRequest, *, timeout: int | None = None) -> OpenCodeResult:
        timeout = timeout or self.settings.opencode_timeout_s
        argv = build_argv(self.settings, req)
        cwd = req.workdir or "."
        log.info(
            "opencode run model=%s variant=%s cwd=%s prompt_len=%d",
            req.model or "(default)", req.variant or "(default)", cwd, len(req.prompt),
        )
        if not self.available:
            return OpenCodeResult(
                127, "", f"opencode binary not found: {self.settings.opencode_binary}", 0.0,
                model=req.model, variant=req.variant, error_kind=ERROR_BINARY_NOT_FOUND,
            )
        start = time.monotonic()
        try:
            # NOTE: env is intentionally inherited (HOME carries opencode auth;
            # proxy/CA settings propagate). Never inject secrets here.
            p = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except Exception as e:
            return OpenCodeResult(1, "", f"failed to start opencode: {e}", 0.0,
                                  model=req.model, variant=req.variant, error_kind=ERROR_SUBPROCESS)
        # wait with cancellation polling
        elapsed = 0.0
        poll = 0.2
        while True:
            if self._cancel.is_set():
                p.kill()
                try:
                    p.wait(timeout=10)
                except Exception:
                    pass
                out, err = "", "cancelled by orchestrator"
                try:
                    o, e = p.communicate(timeout=5)
                    out, err = o or "", e or err
                except Exception:
                    pass
                return OpenCodeResult(130, out[-20000:], err[-20000:], time.monotonic() - start, cancelled=True,
                                      model=req.model, variant=req.variant, error_kind=ERROR_SUBPROCESS)
            try:
                out, err = p.communicate(timeout=poll)
                dur = time.monotonic() - start
                out, err = (out or "")[-20000:], (err or "")[-20000:]
                if p.returncode != 0:
                    kind = classify_error(p.returncode, out, err)
                elif detect_permission_rejection(out, err):
                    kind = ERROR_PERMISSION_REJECTED
                else:
                    kind = ""
                return OpenCodeResult(p.returncode, out, err, dur, model=req.model,
                                      variant=req.variant, error_kind=kind)
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - start
                if elapsed >= timeout:
                    p.kill()
                    try:
                        out, err = p.communicate(timeout=10)
                    except Exception:
                        out, err = "", f"TIMEOUT after {timeout}s"
                    out, err = (out or "")[-20000:], ((err or "") + f"\n[TIMEOUT after {timeout}s]")[-20000:]
                    return OpenCodeResult(124, out, err, elapsed, timed_out=True,
                                          model=req.model, variant=req.variant,
                                          error_kind=ERROR_PROVIDER_TIMEOUT)
                continue

    def run_with_prompt(
        self,
        prompt: str,
        *,
        workdir: str | Path,
        model: str = "",
        variant: str = "",
        timeout: int | None = None,
    ) -> OpenCodeResult:
        return self.run(
            OpenCodeRequest(prompt=prompt, model=model, variant=variant, workdir=str(workdir)),
            timeout=timeout,
        )


class SandboxOpenCodeRunner:
    """Runs OpenCode inside a live SandboxSession (same env as tests).

    The prompt is passed as a direct argv element (no shell), so repository
    content cannot influence quoting. Provider auth stays read-only in the
    session; the host filesystem is not exposed.
    """

    def __init__(self, session, settings: Settings):
        self.session = session
        self.settings = settings

    @property
    def available(self) -> bool:
        return True

    def cancel(self) -> None:  # pragma: no cover - interface parity
        pass

    def reset(self) -> None:  # pragma: no cover - interface parity
        pass

    def run(self, req: OpenCodeRequest, *, timeout: int | None = None) -> OpenCodeResult:
        timeout = timeout or self.settings.opencode_timeout_s
        argv = build_argv(self.settings, req)
        # In the sandbox the binary is on PATH; cwd is the mounted repo.
        res = self.session.exec_argv(
            argv,
            timeout=timeout,
            workdir=self.settings.sandbox_workspace_mount,
        )
        out = (res.stdout or "")[-20000:]
        err = (res.stderr or "")[-20000:]
        if res.timed_out:
            kind = ERROR_PROVIDER_TIMEOUT
        elif res.exit_code != 0:
            kind = classify_error(res.exit_code, out, err)
        elif detect_permission_rejection(out, err):
            kind = ERROR_PERMISSION_REJECTED
        else:
            kind = ""
        return OpenCodeResult(
            res.exit_code, out, err, res.duration_s,
            timed_out=res.timed_out, model=req.model, variant=req.variant, error_kind=kind,
        )

    def run_with_prompt(
        self,
        prompt: str,
        *,
        workdir: str | Path,
        model: str = "",
        variant: str = "",
        timeout: int | None = None,
    ) -> OpenCodeResult:
        return self.run(
            OpenCodeRequest(prompt=prompt, model=model, variant=variant, workdir=str(workdir)),
            timeout=timeout,
        )
