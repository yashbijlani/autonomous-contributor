"""Environment preflight: structured health of the isolated environment.

Runs before OpenCode implementation so toolchain/dependency/resource problems
are classified as environment failures rather than entering the debug loop.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from contributor.sandbox.bootstrap import BootstrapPlan, probe_versions


class EnvironmentHealth(BaseModel):
    healthy: bool = False
    missing_tools: list[str] = Field(default_factory=list)
    wrong_versions: list[str] = Field(default_factory=list)
    dependency_failures: list[str] = Field(default_factory=list)
    resource_warnings: list[str] = Field(default_factory=list)
    network_status: str = "unknown"
    toolchain_versions: dict[str, str] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)


def _require(session, tools: list[str]) -> list[str]:
    missing: list[str] = []
    for tool in tools:
        res = session.exec_shell(f"command -v {tool} >/dev/null 2>&1", timeout=30)
        if res.exit_code != 0:
            missing.append(tool)
    return missing


def run_preflight(
    session,
    plan: BootstrapPlan,
    *,
    bootstrap_report: dict[str, Any] | None = None,
    resource_warnings: list[str] | None = None,
) -> EnvironmentHealth:
    """Verify OS, tools, versions, workspace, resources and network."""
    health = EnvironmentHealth(resource_warnings=list(resource_warnings or []))
    details: dict[str, Any] = {}

    # OS / arch
    details["uname"] = (session.exec_shell("uname -a", timeout=30).stdout or "").strip()[:200]
    details["arch"] = (session.exec_shell("uname -m", timeout=30).stdout or "").strip()

    # Tools per ecosystem
    tool_map = {
        "rust": ["rustup", "cargo", "rustc"],
        "python": ["python"],
        "node": ["node", "npm"],
        "go": ["go"],
        "java": ["java"],
        "ruby": ["ruby"],
    }
    required = list(tool_map.get(plan.ecosystem, [])) + ["opencode", "git"]
    health.missing_tools = _require(session, required)

    # Versions
    health.toolchain_versions = probe_versions(session, plan)

    # Toolchain version check (numeric channels only)
    expected = plan.expected_versions.get("rustc", "")
    actual = health.toolchain_versions.get("rustc", "")
    if expected and expected[0].isdigit() and expected not in actual:
        health.wrong_versions.append(f"rustc expected {expected}, got {actual or 'missing'}")

    # Workspace writability
    ws_res = session.exec_shell(
        f"mkdir -p {session.workspace_mount}/.contributor-scratch && "
        f"touch {session.workspace_mount}/.contributor-scratch/.health && "
        f"rm -f {session.workspace_mount}/.contributor-scratch/.health",
        timeout=30,
    )
    details["workspace_writable"] = ws_res.exit_code == 0

    # Resources
    details["nproc"] = (session.exec_shell("nproc", timeout=30).stdout or "").strip()
    details["memory"] = (session.exec_shell("free -m | head -2", timeout=30).stdout or "").strip()[:200]
    details["disk"] = (session.exec_shell(f"df -Pk {session.workspace_mount} | tail -1", timeout=30).stdout or "").strip()

    # OpenCode + provider network reachability (DNS only; no inference here)
    oc = session.exec_shell("opencode --version", timeout=60)
    details["opencode_version"] = (oc.stdout or "").strip()[:60]
    net = session.exec_shell("getent hosts api.github.com >/dev/null 2>&1 && echo OK || echo FAIL", timeout=30)
    health.network_status = "ok" if "OK" in (net.stdout or "") else "unreachable"

    # Dependency failures
    if bootstrap_report and bootstrap_report.get("failures"):
        health.dependency_failures = list(bootstrap_report["failures"])

    health.details = details
    health.healthy = (
        not health.missing_tools
        and not health.wrong_versions
        and not health.dependency_failures
        and health.network_status == "ok"
        and bool(details.get("workspace_writable"))
    )
    return health
