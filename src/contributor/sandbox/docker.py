"""Docker wrapper. All repo code runs here — never on the host directly.

Security properties:
- workspace bind-mounted read-write at /workspace, nothing else
- no SSH keys / host dotfiles mounted
- no GitHub token injected into the container (git ops needing auth happen on host)
- CPU/memory/pids limits enforced
- optional --network none
- containers are --rm (disposable)
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from contributor.observability.logging import get_logger
from contributor.sandbox.limits import SandboxLimits

log = get_logger("contributor.sandbox.docker")


@dataclass
class ExecResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False


def docker_available() -> bool:
    return shutil.which("docker") is not None


MAX_CAPTURE = 2_000_000  # 2 MB hard cap; full logs are persisted by TestRunner


def _run(cmd: list[str], timeout: int) -> tuple[int, str, str, float, bool]:
    import time

    start = time.monotonic()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        dur = time.monotonic() - start
        return p.returncode, p.stdout[-MAX_CAPTURE:], p.stderr[-MAX_CAPTURE:], dur, False
    except subprocess.TimeoutExpired as e:
        dur = time.monotonic() - start
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else str(e.stdout or "")
        err = (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else str(e.stderr or "")
        return 124, out[-MAX_CAPTURE:], (err + f"\n[TIMEOUT after {timeout}s]")[-MAX_CAPTURE:], dur, True


class DockerSandbox:
    def __init__(self, image: str, limits: SandboxLimits, timeout_s: int = 120):
        self.image = image
        self.limits = limits
        self.timeout_s = timeout_s

    def exec(
        self,
        workspace: Path,
        command: str,
        *,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout_s: int | None = None,
    ) -> ExecResult:
        """Run a shell command inside the container with workspace mounted."""
        self.limits.validate()
        cmd = ["docker", "run", *self.limits.docker_args()]
        # env allowlist — never pass GITHUB_TOKEN / SSH_AUTH_SOCK / AWS_* into container
        blocked_prefixes = ("GITHUB_", "GH_", "SSH_", "AWS_", "OPENAI_", "ANTHROPIC_")
        for k, v in (env or {}).items():
            if k.upper().startswith(blocked_prefixes):
                continue
            cmd += ["-e", f"{k}={v}"]
        cmd += ["-v", f"{workspace.resolve()}:/workspace:rw", "-w", workdir, self.image, "sh", "-c", command]
        code, out, err, dur, timed_out = _run(cmd, timeout_s or self.timeout_s)
        return ExecResult(command=command, exit_code=code, stdout=out, stderr=err, duration_s=dur, timed_out=timed_out)

    def ensure_image(self) -> bool:
        if not docker_available():
            return False
        code, _, _, _, _ = _run(["docker", "image", "inspect", self.image], 30)
        return code == 0
