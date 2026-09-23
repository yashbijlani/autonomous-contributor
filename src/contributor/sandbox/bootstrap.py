"""Repository-aware environment bootstrapping.

Deterministic: the plan is derived from authoritative repository files
(toolchain files, lockfiles, manifests), never from an LLM. The LLM may only
supply hints elsewhere; it cannot choose packages to install.

Priority: devcontainer -> Dockerfile -> CI -> toolchain files -> manifests ->
docs -> ecosystem defaults. (devcontainer/Dockerfile handling is a documented
follow-up; the generic runtime bootstrap below covers toolchain + deps.)
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contributor.models.state import EnvironmentReport

BOOTSTRAP_VERSION = "1"

RUST_TOOLCHAIN_FILES = ("rust-toolchain.toml", "rust-toolchain")
PYTHON_REQUIREMENTS = ("requirements.txt", "requirements-dev.txt", "requirements-test.txt")
PYTHON_LOCKFILES = ("uv.lock", "poetry.lock", "Pipfile.lock", "pdm.lock")
NODE_LOCKFILES = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb")


@dataclass
class BootstrapCommand:
    label: str
    command: str
    timeout_s: int = 1800
    optional: bool = False


@dataclass
class BootstrapPlan:
    ecosystem: str
    commands: list[BootstrapCommand] = field(default_factory=list)
    toolchain: dict[str, str] = field(default_factory=dict)
    expected_versions: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    source: str = "defaults"

    @property
    def is_empty(self) -> bool:
        return not self.commands

    def cache_material(self) -> str:
        parts = [
            BOOTSTRAP_VERSION,
            self.ecosystem,
            ",".join(f"{k}={v}" for k, v in sorted(self.toolchain.items())),
            ",".join(c.command for c in self.commands),
        ]
        return "|".join(parts)


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="ignore")
    except Exception:
        return ""


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except Exception:
        return ""


def _parse_rust_channel(ws: Path) -> str:
    for name in RUST_TOOLCHAIN_FILES:
        p = ws / name
        if not p.exists():
            continue
        text = _read(p)
        if name.endswith(".toml"):
            m = re.search(r'channel\s*=\s*"([^"]+)"', text)
            if m:
                return m.group(1).strip()
        else:
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    cargo = ws / "Cargo.toml"
    if cargo.exists():
        m = re.search(r'rust-version\s*=\s*"([^"]+)"', _read(cargo))
        if m:
            return m.group(1).strip()
    return ""


def _python_test_extras(ws: Path) -> list[str]:
    text = _read(ws / "pyproject.toml")
    if not text:
        return []
    keys = re.findall(r"^\s*([A-Za-z0-9_.-]+)\s*=\s*\[", text, re.MULTILINE)
    wanted = {"test", "tests", "testing", "dev", "development"}
    out: list[str] = []
    for k in keys:
        if k.lower() in wanted and k not in out:
            out.append(k)
    return out


def detect_bootstrap(workspace: Path, report: EnvironmentReport) -> BootstrapPlan:
    """Build a deterministic bootstrap plan from the checkout."""
    langs = set(report.languages)
    notes: list[str] = []

    # Rust is the primary, best-supported ecosystem.
    if "rust" in langs or (workspace / "Cargo.toml").exists():
        channel = _parse_rust_channel(workspace)
        cmds: list[BootstrapCommand] = []
        if channel:
            cmds.append(BootstrapCommand(
                "rustup-toolchain",
                f"rustup toolchain install {channel} --profile minimal --no-self-update",
                timeout_s=1800,
            ))
            cmds.append(BootstrapCommand("rustup-default", f"rustup default {channel}", timeout_s=300))
        else:
            channel = "stable"
            cmds.append(BootstrapCommand(
                "rustup-stable",
                "rustup toolchain install stable --profile minimal --no-self-update",
                timeout_s=1800,
            ))
        return BootstrapPlan(
            ecosystem="rust",
            commands=cmds,
            toolchain={"rust": channel},
            expected_versions={"rustc": channel},
            notes=notes,
            source="rust-toolchain" if channel else "defaults",
        )

    if "python" in langs or any((workspace / f).exists() for f in ("pyproject.toml", "setup.py")):
        cmds = []
        manager = "pip"
        if (workspace / "uv.lock").exists():
            manager = "uv"
        elif (workspace / "poetry.lock").exists():
            manager = "poetry"
        extras = _python_test_extras(workspace)
        extra_suffix = f"[{','.join(extras)}]" if extras else ""
        if manager == "uv":
            cmds.append(BootstrapCommand(
                "uv-sync", "uv sync --frozen --all-extras --all-groups", timeout_s=1800
            ))
            notes.append("uv.lock detected; used uv sync (all extras + groups)")
        elif manager == "poetry":
            cmds.append(BootstrapCommand(
                "poetry-install",
                "python -m pip install --user -q poetry && python -m poetry install --no-interaction",
                timeout_s=1800,
            ))
            notes.append("poetry.lock detected")
        elif (workspace / "pyproject.toml").exists() or (workspace / "setup.py").exists():
            cmds.append(BootstrapCommand(
                "pip-install-project",
                f"python -m pip install --user -e '.{extra_suffix}'",
                timeout_s=1800,
            ))
        for req in PYTHON_REQUIREMENTS:
            if (workspace / req).exists():
                cmds.append(BootstrapCommand(f"pip-{req}", f"python -m pip install --user -r {req}", timeout_s=1800))
        cmds.append(BootstrapCommand("pip-pytest", "python -m pip install --user -q pytest", timeout_s=600, optional=True))
        return BootstrapPlan(
            ecosystem="python",
            commands=cmds,
            toolchain={"python": "system"},
            notes=notes,
            source="python-manifests",
        )

    if "node" in langs or "typescript" in langs or (workspace / "package.json").exists():
        cmds = []
        if (workspace / "package-lock.json").exists():
            cmds.append(BootstrapCommand("npm-ci", "npm ci --no-audit --no-fund", timeout_s=1800))
        elif (workspace / "pnpm-lock.yaml").exists():
            cmds.append(BootstrapCommand("pnpm-install", "corepack enable && pnpm install --frozen-lockfile", timeout_s=1800))
        elif (workspace / "yarn.lock").exists():
            cmds.append(BootstrapCommand("yarn-install", "corepack enable && yarn install --immutable", timeout_s=1800))
        else:
            cmds.append(BootstrapCommand("npm-install", "npm install --no-audit --no-fund", timeout_s=1800))
        return BootstrapPlan(ecosystem="node", commands=cmds, toolchain={"node": "system"}, source="node-lockfile")

    if "go" in langs or (workspace / "go.mod").exists():
        return BootstrapPlan(
            ecosystem="go",
            commands=[BootstrapCommand("go-mod-download", "go mod download", timeout_s=1200)],
            toolchain={"go": "system"},
            source="go.mod",
        )

    if (workspace / "pom.xml").exists():
        return BootstrapPlan(
            ecosystem="java",
            commands=[BootstrapCommand("mvn-go-offline", "mvn -q -B -DskipTests dependency:go-offline", timeout_s=1800)],
            toolchain={"java": "system"},
            source="pom.xml",
        )
    if (workspace / "build.gradle").exists() or (workspace / "build.gradle.kts").exists():
        wrapper = "./gradlew" if (workspace / "gradlew").exists() else "gradle"
        return BootstrapPlan(
            ecosystem="java",
            commands=[BootstrapCommand("gradle-deps", f"{wrapper} --no-daemon dependencies", timeout_s=1800)],
            toolchain={"java": "system"},
            source="gradle",
        )

    if (workspace / "Gemfile").exists():
        return BootstrapPlan(
            ecosystem="ruby",
            commands=[BootstrapCommand("bundle-install", "bundle install", timeout_s=1200)],
            toolchain={"ruby": "system"},
            source="Gemfile",
        )

    return BootstrapPlan(ecosystem="generic", commands=[], source="defaults")


def run_bootstrap(
    session,
    plan: BootstrapPlan,
    *,
    timeout_s: int = 1800,
) -> dict[str, Any]:
    """Execute the plan inside the session; record command/duration/failures."""
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for cmd in plan.commands:
        t0 = time.monotonic()
        res = session.exec_shell(cmd.command, timeout=min(cmd.timeout_s, timeout_s))
        duration = time.monotonic() - t0
        entry = {
            "label": cmd.label,
            "command": cmd.command,
            "exit_code": res.exit_code,
            "duration_s": round(duration, 1),
            "timed_out": res.timed_out,
            "stderr_tail": (res.stderr or "")[-500:],
        }
        results.append(entry)
        if res.exit_code != 0 and not cmd.optional:
            failures.append(f"{cmd.label}: exit {res.exit_code} {entry['stderr_tail'][-200:]}")
            break
    return {
        "ecosystem": plan.ecosystem,
        "source": plan.source,
        "toolchain": plan.toolchain,
        "commands": results,
        "failures": failures,
        "duration_s": round(time.monotonic() - started, 1),
        "ok": not failures,
    }


def probe_versions(session, plan: BootstrapPlan) -> dict[str, str]:
    """Return tool versions observed inside the sandbox."""
    versions: dict[str, str] = {}
    probes = {
        "rustc": "rustc --version",
        "cargo": "cargo --version",
        "python": "python --version",
        "node": "node --version",
        "go": "go version",
        "java": "java -version",
    }
    wanted = set(plan.toolchain)
    if plan.ecosystem == "rust":
        wanted |= {"rustc", "cargo"}
    for name, cmd in probes.items():
        if name not in wanted:
            continue
        res = session.exec_shell(cmd, timeout=60)
        out = (res.stdout or res.stderr or "").strip().splitlines()
        versions[name] = out[0][:120] if out else ""
    return versions
