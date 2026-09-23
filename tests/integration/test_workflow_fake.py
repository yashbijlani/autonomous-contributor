"""Integration: full fake issue -> PR workflow (happy path)."""
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


def test_full_workflow_happy_path(tmp_path: Path, monkeypatch):
    origin = init_origin_repo(tmp_path / "origin", failing=True)
    settings = Settings(
        database_url="sqlite:///:memory:",
        workspaces_root=str(tmp_path / "ws"),
        sandbox_fallback_local=True,
        require_docker=False,
        test_timeout_s=120,
        ci_verify_remote=False,
    )
    db = Database(settings.database_url)
    github = FakeGitHub(origin=origin, ci_state="pass")
    sandbox = __import__("contributor.sandbox.manager", fromlist=["SandboxManager"]).SandboxManager(settings)
    runner = FakeRunner(action=fix_calc_action)
    ctx = WorkflowContext(settings=settings, db=db, github=github, sandbox=sandbox, runner=runner,  # type: ignore[arg-type]
                        execution_mode="live", allow_push=True, allow_create_pr=True)

    import contributor.execution.git as _gitmod
    from contributor.execution.commands import CommandResult as _CR
    monkeypatch.setattr(
        _gitmod, "push_to_target",
        lambda ws, branch, *, url, timeout=300: _CR("git push", 0, "ok", "", 0.0),
    )
    job = JobState(job_id="happy1", repository="o/r", issue_number=1)
    ctx.repo.save_incremental(job)
    final = run_to_completion(ctx, job, max_steps=40)
    assert final.pull_request_url, f"expected PR, got state={final.current_state} errors={final.errors}"
    assert final.current_state.value in ("done", "wait_for_review", "pr_open", "merge_ready")
    assert github.created_prs, "PR should have been created"
    assert final.test_results and final.test_results[-1].passed
