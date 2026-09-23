"""Shared fakes for integration tests (no network, no real GitHub)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


class FakeGitHub:
    def __init__(
        self,
        *,
        origin: Path,
        ci_state: str = "pass",
        pr_comments: list[dict] | None = None,
        ci_states: list[str] | None = None,
        existing_pr: dict | None = None,
        required_checks: list[str] | None = None,
        login: str = "fakeuser",
        allow_push_upstream: bool = False,
        check_run_output: str = "",
        job_logs: str = "",
        rerun_ok: bool = True,
    ):
        self.origin = origin
        self.ci_state = ci_state
        self.ci_states = list(ci_states or [])
        self.pr_comments_list = pr_comments or []
        self.existing_pr = existing_pr
        self.required_checks = list(required_checks or [])
        self.login = login
        self.allow_push_upstream = allow_push_upstream
        self.check_run_output = check_run_output
        self.job_logs = job_logs
        self.rerun_ok = rerun_ok
        self.created_prs: list[dict] = []
        self.updated: list[dict] = []
        self.reruns: list = []
        self._seen_shas: list[str] = []

    def _next_ci_state(self, sha: str) -> str:
        if not self.ci_states:
            return self.ci_state
        if sha not in self._seen_shas:
            self._seen_shas.append(sha)
        idx = min(len(self._seen_shas) - 1, len(self.ci_states) - 1)
        return self.ci_states[idx]

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
            "permissions": {"push": self.allow_push_upstream, "pull": True},
        }

    def get_authenticated_user(self) -> dict[str, Any]:
        return {"login": self.login}

    def get_file(self, owner: str, repo: str, path: str, ref: str = "HEAD") -> str | None:
        return None

    def get_tree(self, owner: str, repo: str, ref: str = "HEAD", recursive: bool = True) -> list[dict]:
        return []

    def search_issues(self, query: str, **kwargs) -> list[dict]:
        return []

    def create_pr(self, owner: str, repo: str, *, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        pr = {
            "html_url": f"https://github.com/{owner}/{repo}/pull/{len(self.created_prs) + 1}",
            "number": len(self.created_prs) + 1,
            "title": title,
            "head": head,
            "base": base,
            "body": body,
            "state": "open",
        }
        self.created_prs.append(pr)
        return pr

    def update_pr(self, owner: str, repo: str, number: int, **kwargs) -> dict[str, Any]:
        self.updated.append({"number": number, **kwargs})
        return {"number": number, "html_url": f"https://github.com/{owner}/{repo}/pull/{number}"}

    def list_pull_requests(self, owner: str, repo: str, *, head: str = "", state: str = "all") -> list[dict]:
        if self.existing_pr:
            prs = [self.existing_pr]
        else:
            prs = [p for p in self.created_prs if p.get("state") == "open"]
        if head:
            prs = [p for p in prs if str(p.get("head", "")) in (head, head.split(":")[-1])]
        return prs

    def list_pr_comments(self, owner: str, repo: str, number: int) -> list[dict]:
        return self.pr_comments_list

    def list_review_comments(self, owner: str, repo: str, number: int) -> list[dict]:
        return []

    def get_branch_required_checks(self, owner: str, repo: str, branch: str) -> list[str]:
        return list(self.required_checks)

    def get_ci_status(self, owner: str, repo: str, sha: str) -> dict[str, Any]:
        state = self._next_ci_state(sha)
        if state == "pass":
            return {"combined": {"state": "success", "statuses": []},
                    "check_runs": [{"name": "t", "status": "completed", "conclusion": "success"}]}
        if state == "fail":
            return {"combined": {"state": "failure", "statuses": []},
                    "check_runs": [{"id": 7, "name": "unit tests", "status": "completed",
                                    "conclusion": "failure",
                                    "details_url": f"https://github.com/{owner}/{repo}/actions/runs/99/job/7"}]}
        if state == "running":
            return {"combined": {"state": "pending", "statuses": []},
                    "check_runs": [{"id": 11, "name": "build", "status": "in_progress", "conclusion": None,
                                    "details_url": "https://github.com/o/r/actions/runs/99/job/11"}]}
        if state == "queued":
            return {"combined": {"state": "pending", "statuses": []},
                    "check_runs": [{"id": 12, "name": "build", "status": "queued", "conclusion": None}]}
        return {"combined": {"state": "pending", "statuses": []}, "check_runs": []}

    def get_check_run(self, owner: str, repo: str, check_run_id) -> dict[str, Any]:
        return {
            "id": check_run_id,
            "output": {"title": "failure", "summary": self.check_run_output, "text": ""},
            "details_url": f"https://github.com/{owner}/{repo}/actions/runs/99/job/{check_run_id}",
        }

    def get_actions_job_logs(self, owner: str, repo: str, job_id, *, max_bytes: int = 200_000) -> str:
        return self.job_logs

    def rerun_actions_run(self, owner: str, repo: str, run_id) -> bool:
        self.reruns.append(run_id)
        return self.rerun_ok

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
