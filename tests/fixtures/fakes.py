"""Shared fakes for integration tests (no network, no real GitHub)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


class FakeGitHub:
    def __init__(self, *, origin: Path, ci_state: str = "pass", pr_comments: list[dict] | None = None):
        self.origin = origin
        self.ci_state = ci_state
        self.pr_comments_list = pr_comments or []
        self.created_prs: list[dict] = []
        self.updated: list[dict] = []

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return {
            "title": "Crash when value is None",
            "body": "Traceback (most recent call last): ... app crashes when value is None. Expected: return 0. " * 2,
            "labels": [{"name": "bug"}],
            "state": "open",
            "user": {"login": "tester"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
            "comments": 0,
            "assignees": [],
        }

    def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return {
            "clone_url": str(self.origin),
            "default_branch": "main",
            "stargazers_count": 10,
            "language": "Python",
        }

    def get_file(self, owner: str, repo: str, path: str, ref: str = "HEAD") -> str | None:
        return None

    def get_tree(self, owner: str, repo: str, ref: str = "HEAD", recursive: bool = True) -> list[dict]:
        return []

    def search_issues(self, query: str, **kwargs) -> list[dict]:
        return []

    def create_pr(self, owner: str, repo: str, *, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        pr = {"html_url": f"https://github.com/{owner}/{repo}/pull/1", "number": 1, "title": title}
        self.created_prs.append(pr)
        return pr

    def update_pr(self, owner: str, repo: str, number: int, **kwargs) -> dict[str, Any]:
        self.updated.append({"number": number, **kwargs})
        return {"number": number}

    def list_pr_comments(self, owner: str, repo: str, number: int) -> list[dict]:
        return self.pr_comments_list

    def list_review_comments(self, owner: str, repo: str, number: int) -> list[dict]:
        return []

    def get_ci_status(self, owner: str, repo: str, sha: str) -> dict[str, Any]:
        if self.ci_state == "pass":
            return {"combined": {"state": "success", "statuses": []},
                    "check_runs": [{"name": "t", "status": "completed", "conclusion": "success"}]}
        if self.ci_state == "fail":
            return {"combined": {"state": "failure", "statuses": [{"context": "t", "state": "failure"}]}, "check_runs": []}
        return {"combined": {"state": "pending", "statuses": []}, "check_runs": []}

    def post_comment(self, owner: str, repo: str, number: int, body: str) -> dict:
        return {}


class FakeRunnerResult:
    def __init__(self, exit_code=0, stdout="ok", stderr="", error_kind="", duration_s=1.0):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.error_kind = error_kind
        self.duration_s = duration_s


class FakeRunner:
    """Mimics OpenCodeRunner.run_with_prompt by executing a scripted action."""

    def __init__(self, action=None):
        self.action = action or (lambda prompt, workdir, model: None)
        self.calls: list[dict] = []

    def run(self, req, *, timeout=None):
        return self.run_with_prompt(req.prompt, workdir=req.workdir, model=req.model)

    def run_with_prompt(self, prompt: str, *, workdir, model="", variant="", timeout=None):
        self.calls.append({"prompt": prompt[:200], "workdir": str(workdir), "model": model, "variant": variant})
        try:
            self.action(prompt, Path(str(workdir)), model)
        except Exception as e:
            return FakeRunnerResult(1, "", str(e))
        return FakeRunnerResult(0, "fake opencode done", "")

    def cancel(self):
        pass


def init_origin_repo(path: Path, *, failing: bool = True) -> Path:
    """Create a local git origin with a tiny python module + pytest test."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "calc.py").write_text('def safe_div(a, b):\n    return a / b\n')
    if failing:
        (path / "test_calc.py").write_text(
            'from calc import safe_div\n'
            'def test_div_by_zero():\n    assert safe_div(1, 0) == 0\n'
        )
    else:
        (path / "test_calc.py").write_text(
            'from calc import safe_div\n'
            'def test_div():\n    assert safe_div(4, 2) == 2\n'
        )
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return path


def fix_calc_action(prompt: str, workdir: Path, model: str):
    """Fake OpenCode: write a minimal fix + keep tests."""
    (workdir / "calc.py").write_text('def safe_div(a, b):\n    if b == 0:\n        return 0\n    return a / b\n')
    # ensure test file exists that passes with the fix
    (workdir / "test_calc.py").write_text(
        'from calc import safe_div\n'
        'def test_div_by_zero():\n    assert safe_div(1, 0) == 0\n'
        'def test_div():\n    assert safe_div(4, 2) == 2\n'
    )
