"""Integration: PR creation only after a verified push; fork/direct targets; reuse."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))

import pytest

import contributor.execution.git as gitmod
from fakes import FakeGitHub

from contributor.config import Settings
from contributor.execution.commands import CommandResult
from contributor.graph.nodes import WorkflowContext, node_create_pr
from contributor.models.state import (
    CIFailureClass,
    CIResult,
    JobState,
    JobStatus,
    ReviewResult,
    ReviewVerdict,
    TestResult,
    TriageDecision,
    TriageResult,
)
from contributor.persistence.database import Database


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True)
    (path / "mod.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    (path / "mod.py").write_text("x = 2\n")
    return path


def _ctx(tmp_path, *, ci_verify=False, allow_create_pr=True, ci_monitor=False,
         push_repo="forkowner/r", benchmark=False) -> WorkflowContext:
    settings = Settings(
        database_url="sqlite:///:memory:",
        workspaces_root=str(tmp_path / "ws"),
        github_token="tok",
        ci_verify_remote=ci_verify,
    )
    return WorkflowContext(
        settings=settings, db=Database(settings.database_url),
        github=None, sandbox=None, runner=None,  # type: ignore[arg-type]
        benchmark_mode=benchmark,
        execution_mode="benchmark" if benchmark else "live",
        allow_push=not benchmark, allow_create_pr=allow_create_pr, ci_monitor=ci_monitor,
        push_repo=push_repo, push_remote="origin",
    )


def _job(tmp_path, ws, *, state=JobStatus.REVIEW) -> JobState:
    j = JobState(
        job_id="j1", repository="o/r", issue_number=1, issue_title="Fix a bug",
        branch_name="contrib/issue-1-abcd", workspace_path=str(ws),
        current_state=state,
        triage_result=TriageResult(decision=TriageDecision.ACCEPT, reason="ok"),
        test_results=[TestResult(command="pytest -q", exit_code=0, passed=True)],
        environment_health={"healthy": True, "missing_tools": []},
        review_result=ReviewResult(verdict=ReviewVerdict.APPROVED, summary="ok"),
        implementation_attempt=1, execution_mode="live",
        issue_metadata={"default_branch": "main"},
    )
    return j


def _publish(ctx, ws, **kw):
    job = _job(None, ws, **kw)
    ctx.repo.save_incremental(job)
    node_create_pr(job.model_dump(mode="python"), ctx)


def _push_ok(monkeypatch, *, code=0, stderr=""):
    monkeypatch.setattr(
        gitmod, "push_to_target",
        lambda ws, branch, *, url, timeout=300: CommandResult("git push", code, "ok" if code == 0 else "", stderr, 0.0),
    )


def _verify_sha(monkeypatch, ws: Path):
    def fake(url, branch, timeout=60):
        r = subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"], capture_output=True, text=True)
        return r.stdout.strip()
    monkeypatch.setattr(gitmod, "remote_ref_sha", fake)


def test_push_failure_creates_no_pr(tmp_path, monkeypatch):
    ws = _repo(tmp_path / "ws" / "job-1")
    ctx = _ctx(tmp_path)
    github = FakeGitHub(origin=ws)
    ctx.github = github
    _push_ok(monkeypatch, code=128, stderr="Permission denied")
    _publish(ctx, ws)
    final = ctx.repo.get("j1")
    assert final.current_state == JobStatus.PUSH_FAILED
    assert github.created_prs == []


def test_unverified_remote_ref_creates_no_pr(tmp_path, monkeypatch):
    ws = _repo(tmp_path / "ws" / "job-1")
    ctx = _ctx(tmp_path, ci_verify=True)
    github = FakeGitHub(origin=ws)
    ctx.github = github
    _push_ok(monkeypatch)
    monkeypatch.setattr(gitmod, "remote_ref_sha", lambda url, branch, timeout=60: "")
    _publish(ctx, ws)
    final = ctx.repo.get("j1")
    assert final.current_state == JobStatus.PUSH_UNVERIFIED
    assert github.created_prs == []


def test_verified_push_creates_pr_with_fork_head(tmp_path, monkeypatch):
    ws = _repo(tmp_path / "ws" / "job-1")
    ctx = _ctx(tmp_path, ci_verify=True, push_repo="forkowner/r")
    github = FakeGitHub(origin=ws)
    ctx.github = github
    _push_ok(monkeypatch)
    _verify_sha(monkeypatch, ws)
    _publish(ctx, ws)
    final = ctx.repo.get("j1")
    assert final.current_state == JobStatus.PR_OPEN
    assert final.remote_ref_verified
    assert github.created_prs[0]["head"] == "forkowner:contrib/issue-1-abcd"
    assert github.created_prs[0]["base"] == "main"


def test_direct_push_repo_uses_branch_head(tmp_path, monkeypatch):
    ws = _repo(tmp_path / "ws" / "job-1")
    ctx = _ctx(tmp_path, ci_verify=True, push_repo="")
    ctx.push_repo = ""
    github = FakeGitHub(origin=ws, allow_push_upstream=True)
    ctx.github = github
    _push_ok(monkeypatch)
    _verify_sha(monkeypatch, ws)
    _publish(ctx, ws)
    final = ctx.repo.get("j1")
    assert final.current_state == JobStatus.PR_OPEN
    assert ":" not in github.created_prs[0]["head"]


def test_existing_pr_is_reused_not_duplicated(tmp_path, monkeypatch):
    ws = _repo(tmp_path / "ws" / "job-1")
    ctx = _ctx(tmp_path, ci_verify=True)
    existing = {"number": 42, "html_url": "https://github.com/o/r/pull/42",
                "state": "open", "head": "contrib/issue-1-abcd"}
    github = FakeGitHub(origin=ws, existing_pr=existing)
    ctx.github = github
    _push_ok(monkeypatch)
    _verify_sha(monkeypatch, ws)
    _publish(ctx, ws)
    final = ctx.repo.get("j1")
    assert final.current_state == JobStatus.PR_OPEN
    assert final.pr_reused and final.pull_request_number == 42
    assert github.created_prs == []
    assert github.updated and github.updated[0]["number"] == 42


def test_benchmark_mode_cannot_create_pr(tmp_path, monkeypatch):
    ws = _repo(tmp_path / "ws" / "job-1")
    ctx = _ctx(tmp_path, benchmark=True, push_repo="forkowner/r")
    github = FakeGitHub(origin=ws)
    ctx.github = github
    pushed = []
    monkeypatch.setattr(gitmod, "push_to_target", lambda *a, **k: pushed.append(1))
    _publish(ctx, ws)
    final = ctx.repo.get("j1")
    assert final.current_state == JobStatus.PUSH_GATE_REJECTED
    assert pushed == []
    assert github.created_prs == []


def test_wrong_repository_is_rejected_by_gate(tmp_path, monkeypatch):
    ws = _repo(tmp_path / "ws" / "job-1")
    ctx = _ctx(tmp_path)
    github = FakeGitHub(origin=ws)
    ctx.github = github
    _push_ok(monkeypatch)
    job = _job(tmp_path, ws)
    job.repository = "someone/else"  # gate compares job.repository to itself
    # The PR base must always be the issue repository; forge a mismatch by
    # making the gate's expected repo differ.
    from contributor.execution.push_gate import run_push_gate

    gate = run_push_gate(job=job, workspace=ws, settings=ctx.settings, allow_push=True,
                         execution_mode="live", target_repo="forkowner/r", remote="origin",
                         expected_repo="o/r")
    assert not gate.allowed


def test_push_to_protected_branch_refused(tmp_path):
    ws = _repo(tmp_path / "ws" / "job-1")
    for name in ("main", "master"):
        with pytest.raises(ValueError):
            gitmod.push_to_target(ws, name, url="https://example.invalid/x.git")
