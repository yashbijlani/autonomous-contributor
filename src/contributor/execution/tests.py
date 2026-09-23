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
import shlex
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
    r"^uv run\b", r"^poetry run\b", r"^pipenv run\b", r"^pdm run\b",
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
    prefix = python_runner_prefix(workspace) if ecosystem == "python" else ""
    for s in suggested or []:
        s = s.strip()
        if s and prefix and not s.startswith(prefix):
            s = prefix + s
        if s and is_command_allowed(s) and s not in cmds:
            cmds.append(s)
    for c in ECOSYSTEM_COMMANDS.get(ecosystem, []):
        if prefix and not c.startswith(prefix):
            c = prefix + c
        if c not in cmds:
            cmds.append(c)
    if ecosystem == "python" and (workspace / "package.json").exists():
        for c in ECOSYSTEM_COMMANDS["node"]:
            if c not in cmds:
                cmds.append(c)
    return cmds[:6]


def _looks_like_test(path: str) -> bool:
    p = path.lower().replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    return (
        "test" in name
        or "spec" in name
        or p.startswith("tests/")
        or p.startswith("test/")
        or "/tests/" in p
        or "/test/" in p
    )


def derive_test_targets(
    workspace: Path,
    changed_files: list[str],
    likely_files: list[str] | None = None,
    *,
    limit: int = 8,
) -> list[str]:
    """Smallest meaningful test set for a change (targeted tests first).

    - test files that were changed/added are used directly
    - for each changed source file, infer conventional test file names
      (test_<stem>.py, <stem>_test.py) and locate them in the workspace
    """
    targets: list[str] = []

    def add(rel: str) -> None:
        rel = rel.strip().lstrip("./")
        if not rel or rel in targets:
            return
        if (workspace / rel).is_file():
            targets.append(rel)

    for f in changed_files:
        if _looks_like_test(f):
            add(f)
    for f in list(changed_files) + list(likely_files or []):
        if _looks_like_test(f):
            continue
        stem = Path(f).stem
        if not stem:
            continue
        names = {f"test_{stem}.py", f"{stem}_test.py"}
        for name in names:
            try:
                for match in workspace.rglob(name):
                    if ".git/" in str(match):
                        continue
                    add(str(match.relative_to(workspace)))
                    if len(targets) >= limit:
                        break
            except Exception:
                continue
            if len(targets) >= limit:
                break
        if len(targets) >= limit:
            break
    return targets[:limit]


def _cargo_crate_targets(changed_files: list[str], likely_files: list[str] | None) -> list[str]:
    crates: list[str] = []
    for f in list(changed_files) + list(likely_files or []):
        parts = f.replace("\\", "/").split("/")
        if len(parts) >= 3 and parts[0] == "crates" and parts[1] not in crates:
            crates.append(parts[1])
    return crates


def _go_package_targets(changed_files: list[str], likely_files: list[str] | None) -> list[str]:
    dirs: list[str] = []
    for f in list(changed_files) + list(likely_files or []):
        f = f.replace("\\", "/")
        if not f.endswith(".go"):
            continue
        d = f.rsplit("/", 1)[0] if "/" in f else "."
        if d not in dirs:
            dirs.append(d)
    return dirs


def _shell_test_targets(
    workspace: Path,
    changed_files: list[str],
    likely_files: list[str] | None,
    *,
    limit: int = 8,
) -> list[str]:
    """Narrowest shell test set for a change.

    Prefers shell test files that were changed/added. When only a source script
    changed (the agent may not have added a test yet), maps the changed
    ``bin/<name>`` script to its conventional ``test/shell.d/*<name>*-test.sh``.
    """
    targets: list[str] = []

    def add(rel: str) -> None:
        rel = rel.strip().lstrip("./")
        if rel and rel not in targets and (workspace / rel).is_file():
            targets.append(rel)

    for f in list(changed_files) + list(likely_files or []):
        f = f.strip().lstrip("./")
        if f.startswith("test/") and f.endswith((".sh", ".bats")):
            add(f)
    if targets:
        return targets[:limit]

    shell_dir = workspace / "test" / "shell.d"
    for f in changed_files:
        f = f.strip().lstrip("./")
        if not f.startswith("bin/"):
            continue
        stem = Path(f).name
        tokens = {stem, stem.removeprefix("omarchy-")}
        for token in sorted(t for t in tokens if t):
            for match in sorted(shell_dir.glob(f"*{token}*-test.sh")):
                add(str(match.relative_to(workspace)))
        if targets:
            break
    return targets[:limit]


def python_runner_prefix(workspace: Path) -> str:
    """Return the project runner prefix for Python tests (project venv aware)."""
    if (workspace / "uv.lock").exists():
        return "uv run "
    if (workspace / "poetry.lock").exists():
        return "poetry run "
    if (workspace / "Pipfile").exists():
        return "pipenv run "
    return ""


def targeted_commands(
    workspace: Path,
    changed_files: list[str],
    likely_files: list[str] | None = None,
    ecosystem: str | None = None,
) -> list[str]:
    """Portable commands that run only the most relevant tests.

    Uses `python -m pytest` (not the host venv path) so the same command works
    inside the Docker sandbox and in local-fallback mode.
    """
    eco = ecosystem or detect_ecosystem(workspace)
    if eco == "python":
        targets = derive_test_targets(workspace, changed_files, likely_files)
        if targets:
            joined = " ".join(shlex.quote(t) for t in targets)
            return [f"{python_runner_prefix(workspace)}python -m pytest -q {joined}"]
    elif eco == "rust":
        crates = _cargo_crate_targets(changed_files, likely_files)
        if crates:
            pkgs = " ".join(f"-p {shlex.quote(c)}" for c in crates)
            return [f"cargo test --quiet {pkgs}"]
    elif eco == "go":
        dirs = _go_package_targets(changed_files, likely_files)
        if dirs:
            pkgs = " ".join(f"./{d}/..." if d != "." else "./..." for d in dirs)
            return [f"go test {pkgs}"]
    elif eco == "shell":
        targets = _shell_test_targets(workspace, changed_files, likely_files)
        if targets:
            joined = " ".join(shlex.quote(t) for t in targets)
            return [f"bash {joined}"]
    return []


_ECO_HINTS: dict[str, tuple[str, ...]] = {
    "python": ("pytest", "python"),
    "rust": ("cargo",),
    "node": ("npm", "pnpm", "yarn", "jest", "vitest", "mocha"),
    "typescript": ("npm", "pnpm", "yarn", "jest", "vitest", "mocha"),
    "go": ("go test",),
    "java": ("mvn", "gradlew", "gradle"),
    "ruby": ("rspec", "rake", "bundle"),
    "php": ("phpunit", "composer"),
    "cpp": ("ctest", "cmake", "make"),
}


def prefer_ecosystem_command(workspace: Path, suggested: list[str] | None) -> str | None:
    """Pick the canonical broader command for the repo's primary ecosystem.

    Prevents running, e.g., `python -m pytest` for a Rust repository just because
    an unrelated Python test script exists.
    """
    eco = detect_ecosystem(workspace)
    hints = _ECO_HINTS.get(eco, ())
    matched = [s for s in (suggested or []) if any(h in s for h in hints)]
    ordered = matched + list(ECOSYSTEM_COMMANDS.get(eco, []))
    prefix = python_runner_prefix(workspace) if eco == "python" else ""
    for c in ordered:
        if prefix and not c.startswith(prefix):
            c = prefix + c
        if is_command_allowed(c):
            return c
    return None


def parse_failures(output: str, limit: int = 40) -> list[str]:
    failures: list[str] = []
    for line in output.splitlines():
        s = line.strip()
        if re.match(r"^(FAILED|FAIL:|✕|×|not ok\b|AssertionError|Error:)", s):
            failures.append(s[:300])
        elif "Traceback (most recent call last)" in line:
            failures.append(s[:300])
        elif any(re.search(p, s, re.IGNORECASE) for p in STRONG_ENV_SIGNALS):
            # Capture missing-tool / toolchain lines so they are classified as
            # environmental instead of silently producing an empty failure set.
            failures.append(s[:300])
        if len(failures) >= limit:
            break
    return failures


# Signals that the sandbox toolchain itself is incompatible with the repo
# (e.g. rustc too old). Deterministically environmental: no code edit can fix it.
TOOLCHAIN_SIGNALS = [
    r"requires rustc",
    r"is not supported by the following packages",
    r"rustc \d+\.\d+(\.\d+)? is not supported",
    r"unsupported rustc",
    r"toolchain .* (not installed|is not installed)",
    r"requires (python|node|go|java|rustc) ?\d",
    r"linker `?[\w.-]+`? not found",
    r"error: linker",
    r"failed to run custom build command",
    r"could not find `?cargo`?",
    r"maturin: command not found",
]

# Signals that a failure is caused by the sandbox missing host tooling rather
# than by the code change (CASE B: REPOSITORY_ENVIRONMENT_INCOMPATIBLE).
STRONG_ENV_SIGNALS = TOOLCHAIN_SIGNALS + [
    r"required command is available",     # explicit missing-tool guard (Omarchy et al)
    r"command not found",
    r"\bnot installed\b",
    r"cannot open display",
    r"no such file or directory",
    r"is not available",
    r"modulenotfounderror",               # missing Python dependency
    r"no module named",
    r"\bimporterror\b",
    r"cannot find module",
    r"module not found",
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
    # Toolchain incompatibility is definitively environmental — an edit cannot fix it.
    if any(re.search(p, blob, re.IGNORECASE) for p in TOOLCHAIN_SIGNALS):
        return "environment"
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

    def __init__(self, sandbox: SandboxManager, test_timeout_s: int = 900, session=None):
        self.sandbox = sandbox
        self.test_timeout_s = test_timeout_s
        # When provided, tests run inside the same long-lived sandbox session
        # that hosts OpenCode and the bootstrapped repository environment.
        self.session = session

    def _write_log(self, workspace: Path, command: str, output: str) -> None:
        """Full test log lives beside the workspace (outside the repo)."""
        try:
            log_path = workspace.parent / f"{workspace.name}.tests.log"
            with log_path.open("a") as f:
                f.write(f"\n===== $ {command} =====\n{output}\n")
        except Exception:
            pass

    def _execute(self, workspace: Path, cmd: str, eco: str) -> tuple[TestResult, bool]:
        if self.session is not None:
            res = self.session.exec_shell(
                cmd, timeout=self.test_timeout_s, workdir=self.session.workspace_mount
            )
        else:
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
        # Only skip to the next candidate when the *runner binary itself* is
        # missing (e.g. `sh: 1: bats: not found`). A suite that ran and failed
        # — even if its output mentions something "not found" — is a real
        # failure and must be reported, not masked by other runners.
        out = (res.stderr + res.stdout).lower()
        first_token = cmd.strip().split()[0].strip("\"'").split("/")[-1]
        runner_missing = res.exit_code == 127 and (
            first_token in out or "command not found" in out or "no such file" in out
        )
        return tr, runner_missing

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
            tr, runner_missing = self._execute(workspace, cmd, eco)
            last = tr
            if tr.passed:
                return tr
            if runner_missing:
                continue
            return tr  # real failure — report it, don't mask with other runners
        if last is None:
            return TestResult(command="(no test command)", exit_code=1, stdout="", stderr="no known test runner", duration_s=0.0, passed=False, failures=["no known test runner"], ecosystem=eco)
        return last

    def run_targeted(
        self,
        workspace: Path,
        *,
        changed_files: list[str],
        likely_files: list[str] | None = None,
        ecosystem: str | None = None,
    ) -> TestResult | None:
        """Run only the smallest meaningful test set for the change.

        Returns None when no targeted command can be derived (caller should
        fall back to the broader repository suite).
        """
        eco = ecosystem or detect_ecosystem(workspace)
        for cmd in targeted_commands(workspace, changed_files, likely_files, eco):
            tr, runner_missing = self._execute(workspace, cmd, eco)
            if runner_missing:
                continue
            return tr
        return None
