"""Deterministic subprocess execution on the host (for orchestrator-owned ops only).

Repository code/tests NEVER run here — they run in SandboxManager. This module
is for git/clone/gh operations owned by the orchestrator.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def run_command(
    command: str | list[str],
    *,
    cwd: str | None = None,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> CommandResult:
    import os

    start = time.monotonic()
    label = command if isinstance(command, str) else " ".join(command)
    try:
        if isinstance(command, str):
            p = subprocess.run(
                ["sh", "-c", command], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                env={**os.environ, **(env or {})},
            )
        else:
            p = subprocess.run(
                command, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                env={**os.environ, **(env or {})},
            )
        return CommandResult(label, p.returncode, p.stdout[-30000:], p.stderr[-30000:], time.monotonic() - start)
    except subprocess.TimeoutExpired:
        return CommandResult(label, 124, "", f"TIMEOUT after {timeout}s", time.monotonic() - start, True)
