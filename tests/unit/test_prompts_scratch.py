"""Unit: worker prompts + scratch dir guidance (permission-rejection prevention)."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))

from fakes import FakeRunnerResult  # noqa: E402

from contributor.config import Settings  # noqa: E402
from contributor.execution.git import SCRATCH_DIR, ensure_scratch_dir  # noqa: E402
from contributor.graph.nodes import WorkflowContext, node_implement  # noqa: E402
from contributor.models.state import JobState  # noqa: E402
from contributor.opencode.prompts import build_implement_prompt  # noqa: E402
from contributor.persistence.database import Database  # noqa: E402


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    return path


def test_ensure_scratch_dir_is_gitignored(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    scratch = ensure_scratch_dir(repo)
    assert scratch.is_dir()
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert SCRATCH_DIR in exclude
    # idempotent
    ensure_scratch_dir(repo)
    assert (repo / ".git" / "info" / "exclude").read_text().count(SCRATCH_DIR) == 1


def test_prompt_forbids_external_paths_and_includes_errors():
    st = JobState(job_id="p", repository="o/r", issue_number=1, issue_title="t", issue_body="b" * 40)
    st.errors.append("permission rejected: use .contributor-scratch/")
    prompt = build_implement_prompt(st)
    assert ".contributor-scratch/" in prompt
    assert "/tmp" in prompt
    assert "Previous attempt errors" in prompt


def test_permission_rejection_is_retryable_not_blocking():
    class _Runner:
        def run_with_prompt(self, prompt, *, workdir, model="", variant="", timeout=None):
            return FakeRunnerResult(0, "no changes", "permission requested: external_directory (/tmp/*); auto-rejecting",
                                    error_kind="PERMISSION_REJECTED")

    settings = Settings(database_url="sqlite:///:memory:")
    db = Database(settings.database_url)
    ctx = WorkflowContext(settings=settings, db=db, github=None, sandbox=None, runner=_Runner())
    job = JobState(job_id="pr1", repository="o/r", issue_number=1)
    ctx.repo.save_incremental(job)
    out = node_implement(job.model_dump(mode="python"), ctx)
    assert out["issue_metadata"].get("provider_blocked") is None
    assert out["implementation_attempt"] == 1
    assert any("permission rejected" in e for e in out["errors"])
