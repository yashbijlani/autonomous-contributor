"""Shared result type for agent (OpenCode) runs."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AgentRunResult:
    code: int
    stdout: str
    stderr: str
    error_kind: str
    model: str
    variant: str
    tier: str
    duration_s: float
