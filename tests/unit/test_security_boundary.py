"""Unit: security boundary — credentials stay out of the sandbox; agent cannot mutate."""
from pathlib import Path

import contributor.execution.git as gitmod
from contributor.models.state import JobState
from contributor.opencode.prompts import build_implement_prompt
from contributor.sandbox.manager import sanitized_env


def test_sanitized_env_strips_github_and_provider_secrets():
    env = sanitized_env({
        "GITHUB_TOKEN": "secret",
        "GH_TOKEN": "secret",
        "ANTHROPIC_API_KEY": "secret",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "PATH": "/usr/bin",
    })
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_pre_push_hook_blocks_sandbox_push(tmp_path: Path):
    workspace = tmp_path / "repo"
    (workspace / ".git" / "hooks").mkdir(parents=True)
    gitmod.block_push(workspace)
    hook = workspace / ".git" / "hooks" / "pre-push"
    assert hook.exists()
    assert "orchestrator owns GitHub mutations" in hook.read_text()
    assert hook.stat().st_mode & 0o111


def test_git_module_has_no_force_push():
    text = Path(gitmod.__file__).read_text()
    assert "--force" not in text
    assert "force=True" not in text


def test_protected_branch_push_refused(tmp_path: Path):
    import pytest

    for branch in ("main", "master"):
        with pytest.raises(ValueError):
            gitmod.push_to_target(tmp_path, branch, url="https://example.invalid/r.git")


def test_agent_prompt_forbids_git_mutations():
    st = JobState(job_id="s1", repository="o/r", issue_number=1)
    prompt = build_implement_prompt(st).lower()
    assert "do not commit" in prompt
    assert "do not push" in prompt
    assert "do not create branches" in prompt


def test_ci_repair_prompt_is_bounded_and_scoped():
    from contributor.opencode.prompts import build_ci_repair_prompt
    from contributor.models.state import CIFailureClass

    st = JobState(job_id="s2", repository="o/r", issue_number=1)
    st.ci_failure_class = CIFailureClass.TEST_FAILURE
    st.ci_diagnosis = {"failed_checks": [{"name": "unit tests", "workflow": "CI",
                                          "conclusion": "failure", "url": "u"}],
                       "error_lines": ["AssertionError: boom"], "jobs": []}
    prompt = build_ci_repair_prompt(st).lower()
    assert "ci repair iteration" in prompt
    assert "smallest correct change" in prompt
    assert "diagnose the failure first" in prompt
