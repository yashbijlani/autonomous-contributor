"""Unit: sandbox session security (mounts/env/user) without Docker."""
import os
from pathlib import Path

from contributor.config import Settings
from contributor.sandbox.resources import SMALL
from contributor.sandbox.session import SandboxSession


def _session(tmp_path: Path) -> SandboxSession:
    ws = tmp_path / "ws"
    ws.mkdir()
    env = tmp_path / "env"
    binp = tmp_path / "opencode"
    binp.write_text("x")
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    cfg = tmp_path / "opencode.json"
    cfg.write_text("{}")
    settings = Settings(
        opencode_host_binary=str(binp),
        opencode_auth_file=str(auth),
        opencode_config_file=str(cfg),
    )
    return SandboxSession(settings, name="t", workspace=ws, env_dir=env, profile=SMALL)


def test_opencode_binary_and_auth_are_readonly(tmp_path):
    s = _session(tmp_path)
    mounts = " ".join(s._mounts())
    assert f":/usr/local/bin/opencode:ro" in mounts
    assert "auth.json" in mounts and ":ro" in mounts
    assert "opencode.json" in mounts and ":ro" in mounts


def test_no_host_secrets_or_socket_mounted(tmp_path):
    s = _session(tmp_path)
    mounts = " ".join(s._mounts())
    assert "docker.sock" not in mounts
    assert ".ssh" not in mounts
    assert ".git-credentials" not in mounts


def test_env_contains_no_credentials(tmp_path):
    s = _session(tmp_path)
    env = " ".join(s._env_args())
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert "AWS_" not in env


def test_runs_as_host_uid_gid(tmp_path):
    s = _session(tmp_path)
    assert s._user() == f"{os.getuid()}:{os.getgid()}"


def test_workspace_and_env_are_the_only_rw_mounts(tmp_path):
    s = _session(tmp_path)
    mounts = s._mounts()
    rw = [m for i, m in enumerate(mounts) if i > 0 and m.endswith(":rw")]
    assert len(rw) == 2
    assert any(m.startswith(str((tmp_path / "ws").resolve())) for m in rw)
    assert any(m.startswith(str((tmp_path / "env").resolve())) for m in rw)
