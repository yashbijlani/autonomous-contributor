"""Git operations owned by the orchestrator (NOT the coding agent).

Rules enforced here:
- never commit to main/master
- branch names are unique per job
- diff/status parsed deterministically (LLM never decides git facts)
"""
from __future__ import annotations

from pathlib import Path

from contributor.execution.commands import CommandResult, run_command


PROTECTED = {"main", "master"}


def clone_repo(clone_url: str, dest: Path, *, timeout: int = 900) -> CommandResult:
    dest.parent.mkdir(parents=True, exist_ok=True)
    return run_command(["git", "clone", clone_url, str(dest)], timeout=timeout)


def checkout_new_branch(workspace: Path, branch: str, *, base: str = "HEAD") -> CommandResult:
    if branch in PROTECTED:
        raise ValueError(f"Refusing to use protected branch name: {branch}")
    return run_command(["git", "-C", str(workspace), "checkout", "-b", branch], timeout=60)


def status_porcelain(workspace: Path) -> CommandResult:
    return run_command(["git", "-C", str(workspace), "status", "--porcelain"], timeout=30)


def diff_stat(workspace: Path, base: str = "HEAD") -> CommandResult:
    return run_command(["git", "-C", str(workspace), "diff", "--stat", base], timeout=30)


def diff_full(workspace: Path, base: str = "HEAD", max_bytes: int = 120000) -> str:
    r = run_command(["git", "-C", str(workspace), "diff", base, "--", ":/", "':!*.lock'"], timeout=30)
    if not r.ok:
        return ""
    out = r.stdout
    if len(out) > max_bytes:
        out = out[:max_bytes] + "\n... [truncated]"
    return out


def changed_files(workspace: Path, base: str = "HEAD") -> list[str]:
    r = run_command(["git", "-C", str(workspace), "diff", "--name-only", base], timeout=30)
    if not r.ok:
        return []
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def working_tree_files(workspace: Path, base: str = "HEAD") -> list[str]:
    """Changed files including untracked ones (git diff misses new files).

    Parses `git status --porcelain` so newly created test files are visible to
    targeted-test selection even before the orchestrator commits anything.
    """
    r = status_porcelain(workspace)
    if not r.ok:
        return []
    out: list[str] = []
    for line in r.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:  # rename: take destination
            path = path.split(" -> ", 1)[1].strip()
        path = path.strip('"')
        if path and path not in out:
            out.append(path)
    return out


def has_changes(workspace: Path) -> bool:
    r = status_porcelain(workspace)
    return r.ok and bool(r.stdout.strip())


def commit_all(workspace: Path, message: str) -> CommandResult:
    if not has_changes(workspace):
        return CommandResult("git commit (noop)", 0, "no changes", "", 0.0)
    r1 = run_command(["git", "-C", str(workspace), "add", "-A"], timeout=60)
    if not r1.ok:
        return r1
    # ensure identity for sandbox clones
    run_command(["git", "-C", str(workspace), "config", "user.email", "contributor@local"], timeout=15)
    run_command(["git", "-C", str(workspace), "config", "user.name", "autonomous-contributor"], timeout=15)
    return run_command(["git", "-C", str(workspace), "commit", "-m", message], timeout=120)


def current_branch(workspace: Path) -> str:
    r = run_command(["git", "-C", str(workspace), "rev-parse", "--abbrev-ref", "HEAD"], timeout=15)
    return r.stdout.strip() if r.ok else ""


def push_branch(workspace: Path, branch: str, *, remote: str = "origin", timeout: int = 300,
                env: dict[str, str] | None = None) -> CommandResult:
    if branch in PROTECTED:
        raise ValueError(f"Refusing to push protected branch: {branch}")
    # --no-verify bypasses the agent-facing pre-push hook; the orchestrator is the
    # only actor with push credentials.
    return run_command(["git", "-C", str(workspace), "push", "--no-verify", "-u", remote, branch],
                       timeout=timeout, env=env)


def head_sha(workspace: Path) -> str:
    r = run_command(["git", "-C", str(workspace), "rev-parse", "HEAD"], timeout=15)
    return r.stdout.strip() if r.ok else ""


def log_last(workspace: Path, n: int = 1) -> str:
    r = run_command(["git", "-C", str(workspace), "log", "--oneline", "-n", str(n)], timeout=15)
    return r.stdout.strip() if r.ok else ""


def diff_full_cached(workspace: Path) -> str:
    r = run_command(["git", "-C", str(workspace), "diff", "--cached"], timeout=30)
    return r.stdout if r.ok else ""


def _redact(text: str) -> str:
    import re

    return re.sub(r"https://[^@\s]+@", "https://<redacted>@", text or "")


def push_to_target(workspace: Path, branch: str, *, url: str, timeout: int = 300) -> CommandResult:
    """Push a contribution branch to an explicit target URL (orchestrator-owned).

    Never force-pushes; refuses protected branch names; redacts credentials from
    the recorded command/output.
    """
    if branch in PROTECTED:
        raise ValueError(f"Refusing to push protected branch: {branch}")
    res = run_command(["git", "-C", str(workspace), "push", "--no-verify", url, branch], timeout=timeout)
    res.command = f"git push --no-verify <redacted-target> {branch}"
    res.stdout = _redact(res.stdout)
    res.stderr = _redact(res.stderr)
    return res


def make_branch_name(job_id: str, issue_number: int) -> str:
    return f"contrib/issue-{issue_number}-{job_id[:8]}"


SCRATCH_DIR = ".contributor-scratch"


def block_push(workspace: Path) -> None:
    """Install a pre-push hook so the agent cannot push; the host orchestrator
    owns all GitHub mutations."""
    hooks = workspace / ".git" / "hooks"
    try:
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "pre-push"
        hook.write_text(
            "#!/bin/sh\n"
            "echo 'push blocked: the orchestrator owns GitHub mutations' >&2\n"
            "exit 1\n"
        )
        hook.chmod(0o755)
    except Exception:
        pass


def ensure_scratch_dir(workspace: Path) -> Path:
    """Create a git-ignored scratch dir so the agent never needs /tmp or $HOME."""
    scratch = workspace / SCRATCH_DIR
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / ".gitkeep").write_text("")
    exclude = workspace / ".git" / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text() if exclude.exists() else ""
        if SCRATCH_DIR not in existing:
            with exclude.open("a") as f:
                f.write(f"\n{SCRATCH_DIR}/\n")
    except Exception:
        pass
    return scratch
