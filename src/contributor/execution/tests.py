"""Multi-ecosystem test detection + execution.

The agent may SUGGEST test commands, but the orchestrator validates them
against an allowlist policy and executes them in the sandbox. Exit codes —
not LLM claims — decide pass/fail.

Supported ecosystems (extensible via TestRunner interface):
python | node | typescript | rust | go | java | c/cpp | ruby | php | generic
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from contributor.models.state import TestResult
from contributor.sandbox.manager import SandboxManager

PYTHON_BIN = sys.executable  # absolute interpreter (venv-aware)

# Commands the orchestrator will auto-run per ecosystem.
ECOSYSTEM_COMMANDS: dict[str, list[str]] = {
    "python": [f"{PYTHON_BIN} -m pytest -q", "python -m pytest -q", "pytest -q"],
    "node": ["npm test --silent", "yarn test --silent", "pnpm test --silent"],
    "typescript": ["npm test --silent"],
    "rust": ["cargo test --quiet"],
    "go": ["go test ./..."],
    "java": ["mvn -q test", "./gradlew test"],
    "cpp": ["cmake --build build && ctest --test-dir build", "make test"],
    "ruby": ["bundle exec rake test", "rspec"],
    "php": ["vendor/bin/phpunit", "composer test"],
    "shell": ["bash test/all", "./test/all", "bats test/"],
}

# Safety: suggested commands must look like test/build commands, not destructive.
ALLOWED_PATTERNS = [
    r"^pytest\b", r"^python\d? -m pytest\b", r"(^|/)python\d? -m pytest\b", r"^npm (test|run .*)\b", r"^yarn test\b",
    r"^pnpm test\b", r"^cargo test\b", r"^go test\b", r"^mvn\b.*test", r"^gradlew? .*\btest",
    r"^ctest\b", r"^cmake\b", r"^make\b", r"^rspec\b", r"^bundle exec\b", r"^phpunit\b",
    r"^composer test\b", r"^npx (jest|vitest|mocha)\b", r"^jest\b", r"^vitest\b", r"^tox\b",
    r"^nox\b", r"(^|/)(python[\d.]*|pytest)(\.exe)?\b",
    r"^bash test/\S+", r"^\./test/\S+", r"^bats\b",
]
BLOCKED_PATTERNS = [
    r"\brm -rf\b", r"\bmkfs\b", r":\(\)\s*\{", r"\bcurl\b.*\|\s*sh\b", r"\bwget\b.*\|\s*sh\b",
    r"\bshutdown\b", r"\breboot\b", r"\bdd\b.*of=/dev", r">\s*/dev/sd",
]


def is_command_allowed(cmd: str) -> bool:
    c = cmd.strip()
    for pat in BLOCKED_PATTERNS:
        if re.search(pat, c):
            return False
    for pat in ALLOWED_PATTERNS:
        if re.search(pat, c):
            return True
    return False


def detect_ecosystem(workspace: Path) -> str:
    files = {p.name for p in workspace.iterdir()} if workspace.exists() else set()

    def has(name: str) -> bool:
        return (workspace / name).exists()

    if has("Cargo.toml"):
        return "rust"
    if has("go.mod"):
        return "go"
    if has("pom.xml") or has("build.gradle") or has("build.gradle.kts"):
        return "java"
    if has("package.json"):
        # peek for typescript
        try:
            pkg = json.loads((workspace / "package.json").read_text()[:8000])
            dev = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "typescript" in dev or has("tsconfig.json"):
                return "typescript"
        except Exception:
            pass
        return "node"
    if has("pyproject.toml") or has("setup.py") or has("setup.cfg") or has("pytest.ini") or has("tox.ini"):
        return "python"
    if has("Gemfile"):
        return "ruby"
    if has("composer.json"):
        return "php"
    if (workspace / "test" / "all").exists() or list(workspace.glob("test/*.bats")):
        return "shell"
    if has("CMakeLists.txt") or has("Makefile"):
        return "cpp"
    # content sniff
    if list(workspace.glob("*.py")):
        return "python"
    if list(workspace.glob("*.rs")):
        return "rust"
    if list(workspace.glob("*.go")):
        return "go"
    return "unknown"


def candidate_commands(workspace: Path, ecosystem: str, suggested: list[str] | None = None) -> list[str]:
    cmds: list[str] = []
    for s in suggested or []:
        s = s.strip()
        if s and is_command_allowed(s) and s not in cmds:
            cmds.append(s)
    for c in ECOSYSTEM_COMMANDS.get(ecosystem, []):
        if c not in cmds:
            cmds.append(c)
    if ecosystem == "python" and (workspace / "package.json").exists():
        for c in ECOSYSTEM_COMMANDS["node"]:
            if c not in cmds:
                cmds.append(c)
    return cmds[:6]


def parse_failures(output: str, limit: int = 40) -> list[str]:
    failures: list[str] = []
    for line in output.splitlines():
        s = line.strip()
        if re.match(r"^(FAILED|FAIL:|✕|×|not ok\b|AssertionError|Error:)", s):
            failures.append(s[:300])
        elif "Traceback (most recent call last)" in line:
            failures.append(s[:300])
        if len(failures) >= limit:
            break
    return failures


# Signals that a failure is caused by the sandbox missing host tooling rather
# than by the code change (CASE B: REPOSITORY_ENVIRONMENT_INCOMPATIBLE).
STRONG_ENV_SIGNALS = [
    r"required command is available",     # explicit missing-tool guard (Omarchy et al)
    r"command not found",
    r"\bnot installed\b",
    r"cannot open display",
    r"no such file or directory",
    r"is not available",
]
ENV_FAILURE_SIGNALS = STRONG_ENV_SIGNALS + [
    r"missing dependenc",
    r"cannot execute",
    r"permission denied",
    r"systemd",
]
# Signals that a failure is a genuine code/assertion failure.
CODE_FAILURE_SIGNALS = [
    r"\bassert", r"expected", r"assertionerror", r"traceback",
    r"!= ", r"panic:", r"segmentation fault",
]


def classify_failures(failures: list[str]) -> str:
    """Return 'environment' | 'code' | 'unknown' for a set of failure lines.

    Heuristic, but conservative: never reports 'environment' when any
    code-level assertion signal is present, and requires either several explicit
    missing-tool failures or a clear majority.
    """
    if not failures:
        return "unknown"
    blob = "\n".join(failures).lower()
    code_hits = sum(1 for p in CODE_FAILURE_SIGNALS if re.search(p, blob))
    if code_hits > 0:
        return "code"
    strong_lines = sum(1 for f in failures if any(re.search(p, f.lower()) for p in STRONG_ENV_SIGNALS))
    if strong_lines >= 3:
        return "environment"
    env_hits = sum(1 for p in ENV_FAILURE_SIGNALS if re.search(p, blob))
    env_lines = sum(1 for f in failures if any(re.search(p, f.lower()) for p in ENV_FAILURE_SIGNALS))
    share = env_lines / len(failures)
    if env_hits >= 2 and share >= 0.5:
        return "environment"
    return "unknown"


class TestRunner:
    """Runs tests inside the sandbox. Deterministic: exit code decides."""

    def __init__(self, sandbox: SandboxManager, test_timeout_s: int = 900):
        self.sandbox = sandbox
        self.test_timeout_s = test_timeout_s

    def _write_log(self, workspace: Path, command: str, output: str) -> None:
        """Full test log lives beside the workspace (outside the repo)."""
        try:
            log_path = workspace.parent / f"{workspace.name}.tests.log"
            with log_path.open("a") as f:
                f.write(f"\n===== $ {command} =====\n{output}\n")
        except Exception:
            pass

    def run(
        self,
        workspace: Path,
        *,
        suggested: list[str] | None = None,
        ecosystem: str | None = None,
    ) -> TestResult:
        eco = ecosystem or detect_ecosystem(workspace)
        last: TestResult | None = None
        for cmd in candidate_commands(workspace, eco, suggested):
            res = self.sandbox.run(workspace, cmd, timeout=self.test_timeout_s)
            combined = res.stdout + "\n" + res.stderr
            # Persist the full log OUTSIDE the repo so it neither pollutes the
            # diff nor gets lost to state truncation (large suites fail early).
            self._write_log(workspace, cmd, combined)
            passed = res.exit_code == 0 and not res.timed_out
            failures = [] if passed else parse_failures(combined)
            classification = "ok" if passed else classify_failures(failures)
            tr = TestResult(
                command=cmd,
                exit_code=res.exit_code,
                stdout=res.stdout[-20000:],
                stderr=res.stderr[-20000:],
                duration_s=res.duration_s,
                timed_out=res.timed_out,
                passed=passed,
                failures=failures,
                ecosystem=eco,
                environment_related=(classification == "environment"),
            )
            last = tr
            if passed:
                return tr
            # Only skip to the next candidate when the *runner binary itself* is
            # missing (e.g. `sh: 1: bats: not found`). A suite that ran and failed
            # — even if its output mentions something "not found" — is a real
            # failure and must be reported, not masked by other runners.
            out = (res.stderr + res.stdout).lower()
            first_token = cmd.strip().split()[0].strip("\"'").split("/")[-1]
            runner_missing = res.exit_code == 127 and (
                first_token in out or "command not found" in out or "no such file" in out
            )
            if runner_missing:
                continue
            return tr  # real failure — report it, don't mask with other runners
        if last is None:
            return TestResult(command="(no test command)", exit_code=1, stdout="", stderr="no known test runner", duration_s=0.0, passed=False, failures=["no known test runner"], ecosystem=eco)
        return last
