"""Integration: full mocked lifecycle issue -> ... -> PR -> CI fail -> repair ->
CI pass -> MERGE_READY, plus resume/reuse and bounded-repair safety."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))

from fakes import FakeGitHub, FakeRunner, init_origin_repo

from contributor.config import Settings
from contributor.graph.nodes import WorkflowContext
from contributor.graph.workflow import resume_entry, run_to_completion
from contributor.models.state import (
    CIFailureClass,
    CIResult,
    JobState,
    JobStatus,
)
from contributor.persistence.database import Database


def _settings(tmp_path) -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        workspaces_root=str(tmp_path / "ws"),
        sandbox_fallback_local=True,
        require_docker=False,
        test_timeout_s=120,
        ci_verify_remote=False,
        ci_poll_interval_s=0,
        ci_max_wait_s=5,
        ci_settle_s=0,
        max_ci_repair_cycles=3,
    )


def _scripted_runner():
    calls = {"n": 0}

    def action(prompt: str, workdir: Path, model: str):
        calls["n"] += 1
        if calls["n"] == 1:
            (workdir / "calc.py").write_text(
                "def safe_div(a, b):\n    if b == 0:\n        return 0\n    return a / b\n")
            (workdir / "test_calc.py").write_text(
                "from calc import safe_div\n"
                "def test_div_by_zero():\n    assert safe_div(1, 0) == 0\n"
                "def test_div():\n    assert safe_div(4, 2) == 2\n")
        else:
            # CI repair: smallest harmless change that keeps tests green.
            p = workdir / "calc.py"
            p.write_text(p.read_text() + "\n# ci repair iteration\n")

    return calls, action


def _ctx(tmp_path, github, runner, *, ci_monitor=True, max_ci=None):
    settings = _settings(tmp_path)
    if max_ci is not None:
        settings = settings.model_copy(update={"max_ci_repair_cycles": max_ci})
    from contributor.sandbox.manager import SandboxManager

    sandbox = SandboxManager(settings)
    sandbox.use_docker = False  # deterministic local execution for tests
    return WorkflowContext(
        settings=settings, db=Database(settings.database_url), github=github,
        sandbox=sandbox, runner=runner,  # type: ignore[arg-type]
        execution_mode="live", allow_push=True, allow_create_pr=True, ci_monitor=ci_monitor,
    )


def _patch_push(monkeypatch):
    import contributor.execution.git as gitmod
    from contributor.execution.commands import CommandResult

    monkeypatch.setattr(
        gitmod, "push_to_target",
        lambda ws, branch, *, url, timeout=300: CommandResult("git push", 0, "ok", "", 0.0),
    )


def test_full_ci_repair_lifecycle(tmp_path, monkeypatch):
    origin = init_origin_repo(tmp_path / "origin", failing=True)
    calls, action = _scripted_runner()
    github = FakeGitHub(origin=origin, ci_states=["fail", "pass"])
    ctx = _ctx(tmp_path, github, FakeRunner(action=action))
    _patch_push(monkeypatch)

    job = JobState(job_id="ci1", repository="o/r", issue_number=1)
    ctx.repo.save_incremental(job)
    final = run_to_completion(ctx, job, max_steps=80)

    assert final.current_state == JobStatus.MERGE_READY, (
        f"state={final.current_state} errors={final.errors}"
    )
    assert final.merge_ready and final.pull_request_url
    assert final.ci_result and final.ci_result.state == "pass"
    assert final.ci_repair_attempt == 1
    assert calls["n"] >= 2
    # Exactly one PR for the contribution branch (reused, never duplicated).
    assert len(github.created_prs) == 1
    assert final.pull_request_number == 1
    assert final.repair_history and final.repair_history[0]["reason"]
    assert final.commit_sha and final.remote_ref_verified


def test_ci_pass_without_repair_reaches_merge_ready(tmp_path, monkeypatch):
    origin = init_origin_repo(tmp_path / "origin", failing=True)
    _, action = _scripted_runner()
    github = FakeGitHub(origin=origin, ci_states=["pass"])
    ctx = _ctx(tmp_path, github, FakeRunner(action=action))
    _patch_push(monkeypatch)
    job = JobState(job_id="ci2", repository="o/r", issue_number=1)
    ctx.repo.save_incremental(job)
    final = run_to_completion(ctx, job, max_steps=60)
    assert final.current_state == JobStatus.MERGE_READY
    assert final.ci_repair_attempt == 0


def test_ci_repair_exhaustion_stops_safely(tmp_path, monkeypatch):
    origin = init_origin_repo(tmp_path / "origin", failing=True)
    calls, action = _scripted_runner()
    # CI never recovers; only 1 repair cycle allowed.
    github = FakeGitHub(origin=origin, ci_state="fail")
    ctx = _ctx(tmp_path, github, FakeRunner(action=action), max_ci=1)
    _patch_push(monkeypatch)
    job = JobState(job_id="ci3", repository="o/r", issue_number=1)
    ctx.repo.save_incremental(job)
    final = run_to_completion(ctx, job, max_steps=80)
    assert final.current_state == JobStatus.CI_REPAIR_EXHAUSTED
    assert final.escalated
    assert final.ci_repair_attempt == 1


def test_infrastructure_failure_never_triggers_code_repair(tmp_path, monkeypatch):
    origin = init_origin_repo(tmp_path / "origin", failing=True)
    calls, action = _scripted_runner()
    github = FakeGitHub(origin=origin, ci_state="fail",
                        job_logs="The runner has received a shutdown signal")
    ctx = _ctx(tmp_path, github, FakeRunner(action=action))
    _patch_push(monkeypatch)
    job = JobState(job_id="ci4", repository="o/r", issue_number=1)
    ctx.repo.save_incremental(job)
    final = run_to_completion(ctx, job, max_steps=60)
    assert final.ci_failure_class == CIFailureClass.CI_INFRASTRUCTURE_FAILURE
    assert final.ci_repair_attempt == 0, "infra failure must not consume repair cycles"
    assert calls["n"] == 1, "OpenCode must not be asked to repair infra failures"
    assert final.current_state == JobStatus.ESCALATE


def test_no_checks_reaches_merge_ready_honestly(tmp_path, monkeypatch):
    """A repo that exposes no checks must not hang; record it and stop."""
    origin = init_origin_repo(tmp_path / "origin", failing=True)
    _, action = _scripted_runner()
    github = FakeGitHub(origin=origin, ci_state="pending")  # pending + zero checks
    ctx = _ctx(tmp_path, github, FakeRunner(action=action))
    _patch_push(monkeypatch)
    job = JobState(job_id="ci6", repository="o/r", issue_number=1)
    ctx.repo.save_incremental(job)
    final = run_to_completion(ctx, job, max_steps=60)
    assert final.current_state == JobStatus.MERGE_READY
    assert final.ci_result and final.ci_result.state == "unknown"
    assert "No GitHub CI checks" in final.human_escalation_reason


def test_resume_entry_mapping():
    st = JobState(job_id="x", repository="o/r", issue_number=1)
    st.current_state = JobStatus.PR_OPEN
    assert resume_entry(st) == "ci_check"
    st.current_state = JobStatus.CI_REPAIRING
    assert resume_entry(st) == "ci_repair"
    st.current_state = JobStatus.CI_FAILED
    assert resume_entry(st) == "ci_check"
    st.current_state = JobStatus.CREATED
    assert resume_entry(st) == "fetch"


def test_resume_after_pr_creation_reuses_pr(tmp_path, monkeypatch):
    """A restart at PR_OPEN must reuse the PR, not open a second one."""
    origin = init_origin_repo(tmp_path / "origin", failing=True)
    _, action = _scripted_runner()
    github = FakeGitHub(origin=origin, ci_states=["pass"])
    ctx = _ctx(tmp_path, github, FakeRunner(action=action))
    _patch_push(monkeypatch)
    job = JobState(job_id="ci5", repository="o/r", issue_number=1)
    ctx.repo.save_incremental(job)
    run_to_completion(ctx, job, max_steps=60)
    assert len(github.created_prs) == 1

    # Simulate an interruption right after PR creation, then resume.
    st = ctx.repo.get("ci5")
    st.done = False
    st.current_state = JobStatus.PR_OPEN
    ctx.repo.save_incremental(st)
    final = run_to_completion(ctx, st, entry=resume_entry(st), max_steps=40)
    assert len(github.created_prs) == 1, "resume must not create a duplicate PR"
    assert final.pull_request_number == 1
