"""Unit: deterministic pre-push gate."""
import subprocess
from pathlib import Path

from contributor.config import Settings
from contributor.execution.push_gate import run_push_gate
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


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True)
    (path / "mod.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    (path / "mod.py").write_text("x = 2\n")  # working-tree change
    return path


def _settings(tmp_path: Path) -> Settings:
    return Settings(workspaces_root=str(tmp_path / "ws"))


def _job(ws: Path) -> JobState:
    return JobState(
        job_id="j1",
        repository="o/r",
        issue_number=1,
        issue_title="fix",
        branch_name="contrib/issue-1-abcd1234",
        workspace_path=str(ws),
        current_state=JobStatus.REVIEW,
        triage_result=TriageResult(decision=TriageDecision.ACCEPT, reason="ok"),
        test_results=[StateTestResult(command="pytest -q", exit_code=0, passed=True)],
        environment_health={"healthy": True, "missing_tools": []},
        review_result=ReviewResult(verdict=ReviewVerdict.APPROVED, summary="ok"),
        implementation_attempt=1,
    )


def _gate(job: JobState, ws: Path, settings: Settings, **over) -> "object":
    kw = dict(
        allow_push=True,
        execution_mode="live",
        target_repo="forkowner/r",
        remote="origin",
        expected_repo="o/r",
    )
    kw.update(over)
    return run_push_gate(job=job, workspace=ws, settings=settings, **kw)


def test_gate_passes_for_valid_live_job(tmp_path):
    ws = _git_repo(tmp_path / "ws" / "job-1")
    settings = _settings(tmp_path)
    res = _gate(_job(ws), ws, settings)
    assert res.allowed, res.reasons


def test_gate_rejects_benchmark_mode(tmp_path):
    ws = _git_repo(tmp_path / "ws" / "job-1")
    res = _gate(_job(ws), ws, _settings(tmp_path), allow_push=False, execution_mode="benchmark")
    assert not res.allowed
    assert any("mode_permits_push" in r for r in res.reasons)


def test_gate_rejects_failed_tests(tmp_path):
    ws = _git_repo(tmp_path / "ws" / "job-1")
    job = _job(ws)
    job.test_results = [StateTestResult(command="pytest -q", exit_code=1, passed=False)]
    assert not _gate(job, ws, _settings(tmp_path)).allowed


def test_gate_rejects_failed_review(tmp_path):
    ws = _git_repo(tmp_path / "ws" / "job-1")
    job = _job(ws)
    job.review_result = ReviewResult(
        verdict=ReviewVerdict.CHANGES_REQUIRED,
        summary="nope",
        issues=[ReviewIssue(severity="blocker", message="bad")],
    )
    reasons = _gate(job, ws, _settings(tmp_path)).reasons
    assert any("review_approved" in r for r in reasons)
    assert any("no_blocking_review_findings" in r for r in reasons)


def test_gate_rejects_environment_failure(tmp_path):
    ws = _git_repo(tmp_path / "ws" / "job-1")
    job = _job(ws)
    job.current_state = JobStatus.ENVIRONMENT_FAILURE
    assert not _gate(job, ws, _settings(tmp_path)).allowed


def test_gate_rejects_resource_incompatible(tmp_path):
    ws = _git_repo(tmp_path / "ws" / "job-1")
    job = _job(ws)
    job.current_state = JobStatus.RESOURCE_INCOMPATIBLE
    assert not _gate(job, ws, _settings(tmp_path)).allowed


def test_gate_rejects_default_branch(tmp_path):
    ws = _git_repo(tmp_path / "ws" / "job-1")
    job = _job(ws)
    job.branch_name = "main"
    assert not _gate(job, ws, _settings(tmp_path)).allowed


def test_gate_rejects_invalid_target(tmp_path):
    ws = _git_repo(tmp_path / "ws" / "job-1")
    assert not _gate(_job(ws), ws, _settings(tmp_path), target_repo="bad target").allowed


def test_gate_rejects_wrong_repo(tmp_path):
    ws = _git_repo(tmp_path / "ws" / "job-1")
    assert not _gate(_job(ws), ws, _settings(tmp_path), expected_repo="other/repo").allowed


def test_gate_passes_for_committed_branch_on_resume(tmp_path):
    """A resumed job whose change is already committed must still pass the gate."""
    ws = _git_repo(tmp_path / "ws" / "job-1")
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-m", "fix"], check=True, capture_output=True)
    job = _job(ws)
    job.commit_sha = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    res = _gate(job, ws, _settings(tmp_path))
    assert res.allowed, res.reasons


def test_gate_rejects_unexpected_secret_file(tmp_path):
    ws = _git_repo(tmp_path / "ws" / "job-1")
    (ws / ".env").write_text("SECRET=1\n")
    reasons = _gate(_job(ws), ws, _settings(tmp_path)).reasons
    assert any("no_secret_or_unexpected_files" in r for r in reasons)


def test_gate_rejects_lockfile_only(tmp_path):
    ws = _git_repo(tmp_path / "ws" / "job-1")
    subprocess.run(["git", "-C", str(ws), "checkout", "--", "mod.py"], check=True, capture_output=True)
    (ws / "poetry.lock").write_text("x")
    reasons = _gate(_job(ws), ws, _settings(tmp_path)).reasons
    assert any("not_lockfile_only" in r for r in reasons)
