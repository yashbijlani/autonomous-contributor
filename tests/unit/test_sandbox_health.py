"""Unit: environment preflight health classification."""
from contributor.sandbox.bootstrap import BootstrapPlan
from contributor.sandbox.docker import ExecResult
from contributor.sandbox.health import run_preflight


class FakeSession:
    workspace_mount = "/workspace/repo"

    def __init__(self, overrides: dict[str, tuple[int, str]] | None = None):
        self.overrides = overrides or {}

    def exec_shell(self, command: str, timeout: int = 60, workdir=None, env=None) -> ExecResult:
        code, out = 0, "OK"
        for key, val in self.overrides.items():
            if key in command:
                code, out = val
                break
        return ExecResult(command=command, exit_code=code, stdout=out, stderr="", duration_s=0.0)


def _rust_plan() -> BootstrapPlan:
    return BootstrapPlan(
        ecosystem="rust",
        toolchain={"rust": "1.98.1"},
        expected_versions={"rustc": "1.98.1"},
    )


def test_healthy_environment():
    session = FakeSession({"rustc --version": (0, "rustc 1.98.1 (abc 2025)")})
    health = run_preflight(session, _rust_plan())
    assert health.healthy
    assert not health.missing_tools
    assert not health.wrong_versions
    assert health.network_status == "ok"


def test_missing_tool_is_unhealthy():
    session = FakeSession({"command -v rustup": (1, "")})
    health = run_preflight(session, _rust_plan())
    assert not health.healthy
    assert "rustup" in health.missing_tools


def test_wrong_version_is_unhealthy():
    session = FakeSession({"rustc --version": (0, "rustc 1.85.1 (old)")})
    health = run_preflight(session, _rust_plan())
    assert not health.healthy
    assert any("1.98.1" in w for w in health.wrong_versions)


def test_bootstrap_failure_is_dependency_failure():
    session = FakeSession()
    health = run_preflight(
        session, _rust_plan(), bootstrap_report={"failures": ["rustup-toolchain: exit 1"]}
    )
    assert not health.healthy
    assert health.dependency_failures


def test_network_unreachable_is_unhealthy():
    session = FakeSession({"getent hosts api.github.com": (1, "FAIL")})
    health = run_preflight(session, _rust_plan())
    assert not health.healthy
    assert health.network_status == "unreachable"
