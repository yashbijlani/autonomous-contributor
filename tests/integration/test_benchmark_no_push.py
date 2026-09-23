"""Integration: benchmark mode terminates at review without push/PR."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))

from fakes import FakeGitHub, FakeRunner, fix_calc_action, init_origin_repo

from contributor.config import Settings
from contributor.graph.nodes import WorkflowContext
from contributor.graph.workflow import run_to_completion
from contributor.models.state import JobState
from contributor.persistence.database import Database
from contributor.sandbox.manager import SandboxManager


def test_benchmark_mode_does_not_create_pr(tmp_path: Path):
    origin = init_origin_repo(tmp_path / "origin", failing=True)
    settings = Settings(
        database_url="sqlite:///:memory:",
        workspaces_root=str(tmp_path / "ws"),
        sandbox_fallback_local=True,
        require_docker=False,
        test_timeout_s=120,
    )
    db = Database(settings.database_url)
    github = FakeGitHub(origin=origin, ci_state="pass")
    sandbox = SandboxManager(settings)
    runner = FakeRunner(action=fix_calc_action)
    ctx = WorkflowContext(
        settings=settings, db=db, github=github, sandbox=sandbox, runner=runner,  # type: ignore[arg-type]
        benchmark_mode=True,
    )
    job = JobState(job_id="bench1", repository="o/r", issue_number=1)
    ctx.repo.save_incremental(job)

    final = run_to_completion(ctx, job, max_steps=40)

    assert final.current_state.value == "done", f"state={final.current_state} errors={final.errors}"
    assert github.created_prs == [], "benchmark mode must never create a PR"
    assert final.pull_request_url == ""
    assert final.review_result is not None
    assert final.review_result.verdict.value == "approved"
    assert final.test_results and final.test_results[-1].passed
    assert final.issue_metadata.get("targeted_test_command"), "targeted test set should be used"
