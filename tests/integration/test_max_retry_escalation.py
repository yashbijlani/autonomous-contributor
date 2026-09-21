"""Integration: max retries -> escalation (no infinite loops)."""
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


def test_max_retry_escalation(tmp_path: Path):
    origin = init_origin_repo(tmp_path / "origin", failing=True)
    settings = Settings(database_url="sqlite:///:memory:", workspaces_root=str(tmp_path / "ws"),
                        sandbox_fallback_local=True, require_docker=False, test_timeout_s=60,
                        max_implementation_attempts=1, max_debug_attempts=1, max_review_cycles=1)
    db = Database(settings.database_url)

    def never_fix(prompt: str, workdir: Path, model: str):
        (workdir / "calc.py").write_text('def safe_div(a, b):\n    return a / b\n')  # still broken

    github = FakeGitHub(origin=origin, ci_state="pass")
    from contributor.sandbox.manager import SandboxManager

    sandbox = SandboxManager(settings)
    runner = FakeRunner(action=never_fix)
    ctx = WorkflowContext(settings=settings, db=db, github=github, sandbox=sandbox, runner=runner)  # type: ignore[arg-type]
    job = JobState(job_id="esc1", repository="o/r", issue_number=1)
    ctx.repo.save_incremental(job)
    final = run_to_completion(ctx, job, max_steps=40)
    assert final.escalated or final.current_state.value in ("escalate", "failed", "done")
    assert final.implementation_attempt <= 2
    assert final.debug_attempt <= 2
