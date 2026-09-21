"""Integration: failing test -> debug -> success (bounded repair)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))

from fakes import FakeGitHub, FakeRunner, init_origin_repo

from contributor.config import Settings
from contributor.graph.nodes import WorkflowContext
from contributor.graph.workflow import run_to_completion
from contributor.models.state import JobState
from contributor.persistence.database import Database


def test_debug_loop_repairs(tmp_path: Path):
    origin = init_origin_repo(tmp_path / "origin", failing=True)
    settings = Settings(database_url="sqlite:///:memory:", workspaces_root=str(tmp_path / "ws"),
                        sandbox_fallback_local=True, require_docker=False, test_timeout_s=120)
    db = Database(settings.database_url)

    calls = {"n": 0}

    def flaky_first(prompt: str, workdir: Path, model: str):
        calls["n"] += 1
        if calls["n"] == 1:
            # bad fix: still fails
            (workdir / "calc.py").write_text('def safe_div(a, b):\n    return a / b\n')
        else:
            (workdir / "calc.py").write_text('def safe_div(a, b):\n    if b == 0:\n        return 0\n    return a / b\n')
            (workdir / "test_calc.py").write_text(
                'from calc import safe_div\ndef test_div_by_zero():\n    assert safe_div(1, 0) == 0\n')

    github = FakeGitHub(origin=origin, ci_state="pass")
    from contributor.sandbox.manager import SandboxManager

    sandbox = SandboxManager(settings)
    runner = FakeRunner(action=flaky_first)
    ctx = WorkflowContext(settings=settings, db=db, github=github, sandbox=sandbox, runner=runner)  # type: ignore[arg-type]
    job = JobState(job_id="debug1", repository="o/r", issue_number=1)
    ctx.repo.save_incremental(job)
    final = run_to_completion(ctx, job, max_steps=60)
    assert calls["n"] >= 2, "debug loop should have retried"
    assert final.pull_request_url, f"expected PR after repair, errors={final.errors}"
