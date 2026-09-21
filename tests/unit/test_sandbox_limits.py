"""Unit: sandbox limits validation + docker args."""
import pytest

from contributor.sandbox.limits import SandboxLimits


def test_valid():
    SandboxLimits(cpu="1.0", memory="2g").validate()


def test_invalid_cpu():
    with pytest.raises(ValueError):
        SandboxLimits(cpu="99", memory="2g").validate()


def test_invalid_memory():
    with pytest.raises(ValueError):
        SandboxLimits(cpu="1.0", memory="2x").validate()


def test_network_none_arg():
    args = SandboxLimits(cpu="1.0", memory="512m", network="none", allow_network=False).docker_args()
    assert "--network" in args and "none" in args


def test_no_secrets_leak_into_docker_env():
    from contributor.sandbox.docker import DockerSandbox

    sb = DockerSandbox(image="x", limits=SandboxLimits())
    # build argv indirectly: ensure blocked prefixes filtered in manager
    from contributor.sandbox.manager import sanitized_env

    env = sanitized_env({"GITHUB_TOKEN": "secret", "FOO": "bar"})
    assert "GITHUB_TOKEN" not in env
    assert env["FOO"] == "bar"
