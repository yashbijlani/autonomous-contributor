"""Unit: deterministic environment discovery + strategy classification."""
from contributor.agents.environment import discover_environment
from contributor.models.state import EnvironmentStrategy


def test_python_repo_is_standard_docker():
    r = discover_environment(
        paths=["pyproject.toml", "src/app.py", "tests/test_app.py"],
        files={"pyproject.toml": "[project]\nname='x'\n"},
        issue_body="fix a bug in app",
    )
    assert "python" in r.languages
    assert r.container_compatible is True
    assert r.strategy == EnvironmentStrategy.STANDARD_DOCKER
    assert any("pytest" in c for c in r.test_commands)


def test_node_repo_detected():
    r = discover_environment(
        paths=["package.json", "src/index.ts", "src/index.test.ts"],
        files={"package.json": '{"scripts": {"test": "jest"}}'},
    )
    assert "typescript" in r.languages
    assert "npm" in r.package_managers
    assert "npm test --silent" in r.test_commands
    assert r.strategy == EnvironmentStrategy.STANDARD_DOCKER


def test_rust_repo_detected():
    r = discover_environment(paths=["Cargo.toml", "src/main.rs"], files={"Cargo.toml": "[package]"})
    assert "rust" in r.languages
    assert "cargo-test" in r.test_frameworks


def test_omarchy_style_repo_is_host_required():
    paths = [
        "test/all", "test/shell.d/menu-test.sh", "install/packaging/all.sh",
        "bin/omarchy-menu-file", "README.md", "Makefile",
    ]
    files = {
        "test/all": "#!/bin/bash\nrequired_command jq\nrequired_command lua\n",
        "Makefile": "test:\n\tmagick -version\n\tsystemctl --version\n",
        "README.md": "Omarchy uses systemctl and sudo pacman.\n",
        "install/packaging/all.sh": "sudo pacman -S hyprland\nupdatedb\nmise install\n",
    }
    r = discover_environment(paths=paths, files=files, issue_body="omarchy-menu-file fails; run with lua")
    assert "shell" in r.languages
    for tool in ("lua", "magick", "jq", "mise", "updatedb"):
        assert tool in r.required_tools, f"missing tool {tool} in {r.required_tools}"
    assert r.requires_systemd is True
    assert r.requires_host_runtime is True
    assert r.container_compatible is False
    assert r.strategy == EnvironmentStrategy.HOST_REQUIRED
    assert r.confidence >= 0.5
    assert "bash test/all" in r.test_commands


def test_privileged_and_host_is_incompatible():
    paths = ["install.sh", "scripts/deploy.sh"]
    files = {
        "install.sh": "sudo chroot /mnt\nmount /dev/sda1\nudevadm trigger\nsystemctl start foo\n",
        "scripts/deploy.sh": "pacman -S docker\n",
    }
    r = discover_environment(paths=paths, files=files, issue_body="")
    assert r.privileged_ops is True
    assert r.unsafe_ops is True
    assert r.requires_host_runtime is True
    assert r.strategy == EnvironmentStrategy.INCOMPATIBLE


def test_empty_repo_defaults_to_standard():
    r = discover_environment(paths=[], files={}, issue_body="")
    assert r.container_compatible is True
    assert r.strategy == EnvironmentStrategy.STANDARD_DOCKER
    assert r.confidence <= 0.3
