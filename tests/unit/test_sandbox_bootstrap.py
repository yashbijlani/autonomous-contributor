"""Unit: deterministic repository environment detection."""
from pathlib import Path

from contributor.models.state import EnvironmentReport
from contributor.sandbox.bootstrap import detect_bootstrap


def _ws(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp_path


def test_rust_toolchain_toml(tmp_path):
    ws = _ws(tmp_path, {"rust-toolchain.toml": '[toolchain]\nchannel = "1.98.1"\n'})
    plan = detect_bootstrap(ws, EnvironmentReport(languages=["rust"]))
    assert plan.ecosystem == "rust"
    assert plan.toolchain["rust"] == "1.98.1"
    assert any("rustup toolchain install 1.98.1" in c.command for c in plan.commands)
    assert plan.expected_versions["rustc"] == "1.98.1"


def test_rust_plain_toolchain_file(tmp_path):
    ws = _ws(tmp_path, {"rust-toolchain": "nightly-2025-01-01\n"})
    plan = detect_bootstrap(ws, EnvironmentReport(languages=["rust"]))
    assert plan.toolchain["rust"] == "nightly-2025-01-01"


def test_rust_cargo_rust_version_fallback(tmp_path):
    ws = _ws(tmp_path, {"Cargo.toml": '[package]\nname="x"\nrust-version = "1.96.0"\n'})
    plan = detect_bootstrap(ws, EnvironmentReport(languages=["rust"]))
    assert plan.toolchain["rust"] == "1.96.0"


def test_python_requirements(tmp_path):
    ws = _ws(tmp_path, {"requirements.txt": "flask\n", "requirements-dev.txt": "pytest\n"})
    plan = detect_bootstrap(ws, EnvironmentReport(languages=["python"]))
    cmds = " ".join(c.command for c in plan.commands)
    assert "pip install --user -r requirements.txt" in cmds
    assert "pip install --user -r requirements-dev.txt" in cmds


def test_python_pyproject_with_test_extra(tmp_path):
    ws = _ws(tmp_path, {"pyproject.toml": '[project]\nname="x"\n[project.optional-dependencies]\ntest = ["pytest"]\n'})
    plan = detect_bootstrap(ws, EnvironmentReport(languages=["python"]))
    cmds = " ".join(c.command for c in plan.commands)
    assert "pip install --user -e '.[test]'" in cmds


def test_python_uv_lock_prefers_uv(tmp_path):
    ws = _ws(tmp_path, {"uv.lock": "", "pyproject.toml": '[project]\nname="x"\n'})
    plan = detect_bootstrap(ws, EnvironmentReport(languages=["python"], package_managers=["uv"]))
    assert plan.ecosystem == "python"
    assert any("uv sync" in c.command for c in plan.commands)


def test_node_npm_ci(tmp_path):
    ws = _ws(tmp_path, {"package.json": "{}", "package-lock.json": "{}"})
    plan = detect_bootstrap(ws, EnvironmentReport(languages=["javascript"]))
    assert plan.ecosystem == "node"
    assert plan.commands[0].command.startswith("npm ci")


def test_node_pnpm(tmp_path):
    ws = _ws(tmp_path, {"package.json": "{}", "pnpm-lock.yaml": ""})
    plan = detect_bootstrap(ws, EnvironmentReport(languages=["typescript"]))
    assert "pnpm install" in plan.commands[0].command


def test_go_mod(tmp_path):
    ws = _ws(tmp_path, {"go.mod": "module x\n"})
    plan = detect_bootstrap(ws, EnvironmentReport(languages=["go"]))
    assert plan.ecosystem == "go"
    assert plan.commands[0].command == "go mod download"


def test_cache_material_changes_with_toolchain(tmp_path):
    ws1 = _ws(tmp_path / "a", {"rust-toolchain.toml": '[toolchain]\nchannel = "1.98.1"\n'})
    ws2 = _ws(tmp_path / "b", {"rust-toolchain.toml": '[toolchain]\nchannel = "1.85.0"\n'})
    p1 = detect_bootstrap(ws1, EnvironmentReport(languages=["rust"]))
    p2 = detect_bootstrap(ws2, EnvironmentReport(languages=["rust"]))
    assert p1.cache_material() != p2.cache_material()


def test_empty_repo_is_generic(tmp_path):
    plan = detect_bootstrap(tmp_path, EnvironmentReport())
    assert plan.ecosystem == "generic"
    assert plan.is_empty
