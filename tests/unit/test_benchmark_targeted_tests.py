"""Unit: smallest-meaningful-test-set derivation."""
from pathlib import Path

from contributor.execution.tests import (
    derive_test_targets,
    is_command_allowed,
    targeted_commands,
)


def _mk(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text("def test_f():\n    assert True\n")
    (tmp_path / "tests" / "test_other.py").write_text("def test_o():\n    assert True\n")
    return tmp_path


def test_changed_source_maps_to_test_file(tmp_path):
    ws = _mk(tmp_path)
    targets = derive_test_targets(ws, ["pkg/mod.py"])
    assert "tests/test_mod.py" in targets


def test_changed_test_file_used_directly(tmp_path):
    ws = _mk(tmp_path)
    targets = derive_test_targets(ws, ["tests/test_other.py"])
    assert "tests/test_other.py" in targets


def test_no_targets_when_nothing_related(tmp_path):
    ws = _mk(tmp_path)
    assert derive_test_targets(ws, ["README.md"]) == []


def test_targeted_command_is_portable_and_allowed(tmp_path):
    ws = _mk(tmp_path)
    cmds = targeted_commands(ws, ["pkg/mod.py"], ecosystem="python")
    assert cmds == ["python -m pytest -q tests/test_mod.py"]
    assert is_command_allowed(cmds[0])


def test_uv_project_prefixes_runner(tmp_path):
    ws = _mk(tmp_path)
    (ws / "uv.lock").write_text("")
    cmds = targeted_commands(ws, ["pkg/mod.py"], ecosystem="python")
    assert cmds == ["uv run python -m pytest -q tests/test_mod.py"]
    assert is_command_allowed(cmds[0])


def test_poetry_project_prefixes_runner(tmp_path):
    ws = _mk(tmp_path)
    (ws / "poetry.lock").write_text("")
    cmds = targeted_commands(ws, ["pkg/mod.py"], ecosystem="python")
    assert cmds == ["poetry run python -m pytest -q tests/test_mod.py"]
    assert is_command_allowed(cmds[0])


def test_targeted_commands_empty_for_unmappable_ecosystem(tmp_path):
    ws = _mk(tmp_path)
    assert targeted_commands(ws, ["README.md"], ecosystem="unknown") == []


def test_rust_crate_targets(tmp_path):
    ws = _mk(tmp_path)
    cmds = targeted_commands(
        ws, ["crates/uv-python/src/interpreter.rs"], ecosystem="rust"
    )
    assert cmds == ["cargo test --quiet -p uv-python"]
    assert is_command_allowed(cmds[0])


def test_go_package_targets(tmp_path):
    ws = _mk(tmp_path)
    cmds = targeted_commands(ws, ["internal/resolver/resolver.go"], ecosystem="go")
    assert cmds == ["go test ./internal/resolver/..."]
    assert is_command_allowed(cmds[0])


def test_shell_changed_test_file_is_targeted(tmp_path):
    ws = _mk(tmp_path)
    shell_dir = ws / "test" / "shell.d"
    shell_dir.mkdir(parents=True)
    test_file = shell_dir / "menu-file-symlink-test.sh"
    test_file.write_text("#!/bin/bash\necho ok\n")
    cmds = targeted_commands(
        ws, ["bin/omarchy-menu-file", "test/shell.d/menu-file-symlink-test.sh"], ecosystem="shell"
    )
    assert cmds == ["bash test/shell.d/menu-file-symlink-test.sh"]
    assert is_command_allowed(cmds[0])


def test_shell_source_maps_to_conventional_test(tmp_path):
    ws = _mk(tmp_path)
    shell_dir = ws / "test" / "shell.d"
    shell_dir.mkdir(parents=True)
    (shell_dir / "menu-file-test.sh").write_text("#!/bin/bash\necho ok\n")
    cmds = targeted_commands(ws, ["bin/omarchy-menu-file"], ecosystem="shell")
    assert cmds == ["bash test/shell.d/menu-file-test.sh"]
    assert is_command_allowed(cmds[0])


def test_shell_no_targets_without_test(tmp_path):
    ws = _mk(tmp_path)
    assert targeted_commands(ws, ["bin/omarchy-menu-file"], ecosystem="shell") == []
