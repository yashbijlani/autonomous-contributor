"""Workspace lifecycle manager.

- Creates disposable per-job workspaces under workspaces_root.
- Prefers Docker exec; falls back to local subprocess execution ONLY when
  docker is unavailable and sandbox_fallback_local is True (tests/dev).
  Local fallback still restricts env (no secrets) and enforces timeouts.
- Cleanup removes the workspace directory.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from contributor.config import Settings, get_settings, resolve_workspace_root
from contributor.observability.logging import get_logger
from contributor.sandbox.docker import DockerSandbox, ExecResult, docker_available
from contributor.sandbox.limits import SandboxLimits

log = get_logger("contributor.sandbox.manager")

SECRET_PREFIXES = ("GITHUB_", "GH_", "SSH_", "AWS_", "OPENAI_", "ANTHROPIC_", "GITLAB_")


def sanitized_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    import sys as _sys
    from pathlib import Path as _P

    # Keep PATH functional in local-fallback mode by including the current
    # interpreter's bin dir (venv) first, then the minimal safe default.
    venv_bin = str(_P(_sys.executable).parent)
    env = {
        "PATH": f"{venv_bin}:/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "CI": "1",
    }
    for k, v in (extra or {}).items():
        if k.upper().startswith(SECRET_PREFIXES):
            continue
        env[k] = v
    return env


class SandboxManager:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.root = resolve_workspace_root(self.settings)
        self.limits = SandboxLimits.from_settings(self.settings)
        self.docker = DockerSandbox(
            image=self.settings.sandbox_image,
            limits=self.limits,
            timeout_s=self.settings.docker_timeout_s,
        )
        self.use_docker = docker_available() and self.docker.ensure_image()
        if self.settings.require_docker and not self.use_docker:
            raise RuntimeError("Docker required (REQUIRE_DOCKER=1) but unavailable or image missing")
        if not self.use_docker and not self.settings.sandbox_fallback_local:
            raise RuntimeError("Docker unavailable and local fallback disabled")

    def create_workspace(self, job_id: str) -> Path:
        ws = self.root / f"job-{job_id}"
        ws.mkdir(parents=True, exist_ok=True)
        log.info("workspace created %s", ws)
        return ws

    def run(
        self,
        workspace: Path,
        command: str,
        *,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        timeout = timeout or self.settings.command_timeout_s
        if self.use_docker:
            # docker path: secrets never enter container (sanitized inside DockerSandbox too).
            # The caller timeout (e.g. TestRunner's test_timeout_s) must reach `docker run`.
            return self.docker.exec(workspace, command, env=sanitized_env(env), timeout_s=timeout)
        # local fallback (dev/tests)
        start = time.monotonic()
        try:
            p = subprocess.run(
                ["sh", "-c", command],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, **sanitized_env(env)},
            )
            dur = time.monotonic() - start
            return ExecResult(
                command=command,
                exit_code=p.returncode,
                stdout=p.stdout[-20000:],
                stderr=p.stderr[-20000:],
                duration_s=dur,
            )
        except subprocess.TimeoutExpired as e:
            dur = time.monotonic() - start
            return ExecResult(
                command=command, exit_code=124, stdout="", stderr=f"TIMEOUT after {timeout}s", duration_s=dur, timed_out=True
            )

    def cleanup(self, workspace: Path) -> None:
        try:
            # safety: only delete inside workspaces root
            rp = workspace.resolve()
            if self.root.resolve() not in rp.parents and rp != self.root.resolve():
                log.error("refusing to cleanup path outside workspaces root: %s", rp)
                return
            shutil.rmtree(rp, ignore_errors=True)
            log.info("workspace cleaned %s", rp)
        except Exception as e:
            log.warning("cleanup failed %s: %s", workspace, e)
