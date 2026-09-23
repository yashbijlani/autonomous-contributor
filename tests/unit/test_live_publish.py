"""Unit: live contribution mode — benchmark cannot push, live can, push results persist."""
import subprocess
from pathlib import Path

import contributor.execution.git as gitmod
from contributor.config import Settings
from contributor.execution.commands import CommandResult
from contributor.graph.nodes import WorkflowContext, node_create_pr
from contributor.models.state import (
    JobState,
    JobStatus,
    ReviewIssue,
    ReviewResult,
    ReviewVerdict,
    TestResult as StateTestResult,
    TriageDecision,
    TriageResult,
)
from contributor.persistence.database import Database


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True)
    (path / "mod.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    (path / "mod.py").write_text("x = 2\n")
    return path


def _ctx(tmp_path: Path, *, mode: str, allow_push: bool) -> tuple[WorkflowContext, JobState, Path]:
    ws = _git_repo(tmp_path / "ws" / "job-1")
    settings = Settings(
        database_url="sqlite:///:memory:",
        workspaces_root=str(tmp_path / "ws"),
        github_token="tok",
    )
    ctx = WorkflowContext(
        settings=settings,
        db=Database(settings.database_url),
        github=None, sandbox=None, runner=None,  # type: ignore[arg-type]
        execution_mode=mode,
        allow_push=allow_push,
        allow_create_pr=False,
        push_repo="forkowner/r",
        push_remote="origin",
    )
    job = JobState(
        job_id="j1",
        repository="o/r",
        issue_number=1,
        issue_title="Fix a bug",
        branch_name="contrib/issue-1-abcd1234",
        workspace_path=str(ws),
        current_state=JobStatus.REVIEW,
        triage_result=TriageResult(decision=TriageDecision.ACCEPT, reason="ok"),
        test_results=[StateTestResult(command="pytest -q", exit_code=0, passed=True)],
        environment_health={"healthy": True, "missing_tools": []},
        review_result=ReviewResult(verdict=ReviewVerdict.APPROVED, summary="ok"),
        implementation_attempt=1,
        execution_mode=mode,
    )
    ctx.repo.save_incremental(job)
    return ctx, job, ws


def test_benchmark_mode_cannot_push(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gitmod, "push_to_target", lambda *a, **k: calls.append(1))
    ctx, job, _ = _ctx(tmp_path, mode="benchmark", allow_push=False)
    node_create_pr(job.model_dump(mode="python"), ctx)
    final = ctx.repo.get("j1")
    assert final.current_state == JobStatus.PUSH_GATE_REJECTED
    assert calls == []


def test_live_mode_gate_rejection_does_not_push(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gitmod, "push_to_target", lambda *a, **k: calls.append(1))
    ctx, job, _ = _ctx(tmp_path, mode="live", allow_push=True)
    job.test_results = [StateTestResult(command="pytest -q", exit_code=1, passed=False)]
    ctx.repo.save_incremental(job)
    node_create_pr(job.model_dump(mode="python"), ctx)
    final = ctx.repo.get("j1")
    assert final.current_state == JobStatus.PUSH_GATE_REJECTED
    assert calls == []
    assert final.push_gate and not final.push_gate["allowed"]


def test_live_mode_successful_push_is_persisted(tmp_path, monkeypatch):
    def fake_push(ws, branch, *, url, timeout=300):
        return CommandResult("git push --no-verify <redacted> " + branch, 0, "ok", "", 0.0)

    monkeypatch.setattr(gitmod, "push_to_target", fake_push)
    ctx, job, _ = _ctx(tmp_path, mode="live", allow_push=True)
    node_create_pr(job.model_dump(mode="python"), ctx)
    final = ctx.repo.get("j1")
    assert final.current_state == JobStatus.PUSHED
    assert final.done
    assert final.commit_sha
    assert final.push_result and final.push_result["ok"]
    assert final.push_result["target"] == "forkowner/r"
    assert final.execution_mode == "live"


def test_live_mode_push_failure_is_distinct(tmp_path, monkeypatch):
    def fake_push(ws, branch, *, url, timeout=300):
        return CommandResult("git push", 128, "", "remote: Permission denied", 0.0)

    monkeypatch.setattr(gitmod, "push_to_target", fake_push)
    ctx, job, _ = _ctx(tmp_path, mode="live", allow_push=True)
    node_create_pr(job.model_dump(mode="python"), ctx)
    final = ctx.repo.get("j1")
    assert final.current_state == JobStatus.PUSH_FAILED
    assert final.push_result and not final.push_result["ok"]
    assert "Permission denied" in final.human_escalation_reason


def test_committed_branch_references_issue(tmp_path, monkeypatch):
    monkeypatch.setattr(
        gitmod, "push_to_target",
        lambda ws, branch, *, url, timeout=300: CommandResult("git push", 0, "ok", "", 0.0),
    )
    ctx, job, ws = _ctx(tmp_path, mode="live", allow_push=True)
    node_create_pr(job.model_dump(mode="python"), ctx)
    log = subprocess.run(["git", "-C", str(ws), "log", "-1", "--pretty=%s"], capture_output=True, text=True).stdout
    assert "#1" in log
