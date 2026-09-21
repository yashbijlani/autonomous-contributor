"""Resource limits + validation for sandboxed execution."""
from __future__ import annotations

from dataclasses import dataclass

from contributor.config import Settings


@dataclass(frozen=True)
class SandboxLimits:
    cpu: str = "1.0"
    memory: str = "2g"
    pids_limit: int = 256
    network: str = "bridge"
    allow_network: bool = True
    timeout_s: int = 600

    @classmethod
    def from_settings(cls, s: Settings) -> SandboxLimits:
        return cls(
            cpu=s.sandbox_cpu,
            memory=s.sandbox_memory,
            pids_limit=s.sandbox_pids_limit,
            network=s.sandbox_network,
            allow_network=s.sandbox_allow_network,
            timeout_s=s.command_timeout_s,
        )

    def docker_args(self) -> list[str]:
        args = [
            "--cpus",
            self.cpu,
            "--memory",
            self.memory,
            "--pids-limit",
            str(self.pids_limit),
            "--rm",
        ]
        if not self.allow_network or self.network == "none":
            args += ["--network", "none"]
        elif self.network and self.network != "bridge":
            args += ["--network", self.network]
        return args

    def validate(self) -> None:
        try:
            cpus = float(self.cpu)
        except ValueError:
            raise ValueError(f"Invalid CPU limit: {self.cpu!r}")
        if not (0.1 <= cpus <= 32):
            raise ValueError(f"CPU limit out of range: {self.cpu!r}")
        mem = self.memory.lower()
        if not (mem.endswith("m") or mem.endswith("g")):
            raise ValueError(f"Memory limit must end in m/g: {self.memory!r}")
        if self.pids_limit < 16:
            raise ValueError("pids_limit too low")
