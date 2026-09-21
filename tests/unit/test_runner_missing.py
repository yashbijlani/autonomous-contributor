"""Unit: runner-missing vs suite-failure classification in TestRunner."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from contributor.execution.tests import candidate_commands, detect_ecosystem
from contributor.sandbox.docker import ExecResult


def _tr(cmd: str, code: int, out: str = "", err: str = "") -> ExecResult:
    return ExecResult(command=cmd, exit_code=code, stdout=out, stderr=err, duration_s=1.0)


def _classify(cmd: str, res: ExecResult) -> bool:
    """Mirror of TestRunner.run's runner_missing logic."""
    out = (res.stderr + res.stdout).lower()
    first_token = cmd.strip().split()[0].strip("\"'").split("/")[-1]
    return res.exit_code == 127 and (
        first_token in out or "command not found" in out or "no such file" in out
    )


def test_missing_runner_detected():
    assert _classify("bats test/", _tr("bats test/", 127, "", "sh: 1: bats: not found")) is True


def test_suite_failure_not_masked():
    # Suite ran 4 min, failed, output mentions something else "not found".
    assert (
        _classify(
            "bash test/all", _tr("bash test/all", 1, "tool foo not found in container", "")
        )
        is False
    )


def test_shell_ecosystem_detected(tmp_path: Path):
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "all").write_text("#!/bin/bash\n")
    assert detect_ecosystem(tmp_path) == "shell"
    assert "bash test/all" in candidate_commands(tmp_path, "shell")
