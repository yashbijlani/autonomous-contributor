"""Unit: caller timeouts must propagate to `docker run` (not silently capped)."""
from pathlib import Path

from contributor.config import Settings
from contributor.sandbox.docker import DockerSandbox, ExecResult
from contributor.sandbox.limits import SandboxLimits
from contributor.sandbox.manager import SandboxManager


def test_docker_exec_forwards_timeout(monkeypatch):
    seen: dict = {}

    def fake_run(cmd, timeout):
        seen["timeout"] = timeout
        return 0, "out", "err", 1.0, False

    import contributor.sandbox.docker as dock

    monkeypatch.setattr(dock, "_run", fake_run)
    sb = DockerSandbox(image="img", limits=SandboxLimits(), timeout_s=120)
    sb.exec(Path("/tmp"), "echo hi", timeout_s=900)
    assert seen["timeout"] == 900
    sb.exec(Path("/tmp"), "echo hi")
    assert seen["timeout"] == 120


def test_manager_forwards_test_timeout(monkeypatch):
    seen: dict = {}

    class FakeDocker:
        def exec(self, workspace, command, *, workdir="/workspace", env=None, timeout_s=None):
            seen["timeout_s"] = timeout_s
            return ExecResult(command, 0, "", "", 0.0)

    s = Settings(
        database_url="sqlite:///:memory:",
        workspaces_root="/tmp/ws-test-timeout",
        sandbox_fallback_local=True,
        require_docker=False,
        test_timeout_s=900,
    )
    mgr = SandboxManager.__new__(SandboxManager)
    mgr.settings = s
    mgr.use_docker = True
    mgr.docker = FakeDocker()  # type: ignore[assignment]
    mgr.run(Path("/tmp"), "bash test/all", timeout=900)
    assert seen["timeout_s"] == 900
