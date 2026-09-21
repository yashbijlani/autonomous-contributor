"""Optional MCP tool layer. Not required for orchestration.

Provides small, scoped tool sets per agent so context stays lean:
- triage: github-read tools
- implement: workspace tools (via OpenCode, not MCP exec)
- reviewer: diff/test tools
This module only defines the interface + stdio config helpers; real MCP
servers (github, filesystem) are launched externally when configured.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MCPServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class MCPToolPolicy:
    agent: str
    allowed_servers: list[str] = field(default_factory=list)
    max_tools: int = 8


DEFAULT_POLICIES = [
    MCPToolPolicy(agent="triage", allowed_servers=["github"], max_tools=6),
    MCPToolPolicy(agent="planner", allowed_servers=["github", "filesystem"], max_tools=8),
    MCPToolPolicy(agent="reviewer", allowed_servers=["github"], max_tools=6),
]


def policy_for(agent: str) -> MCPToolPolicy:
    for p in DEFAULT_POLICIES:
        if p.agent == agent:
            return p
    return MCPToolPolicy(agent=agent, allowed_servers=[], max_tools=4)
