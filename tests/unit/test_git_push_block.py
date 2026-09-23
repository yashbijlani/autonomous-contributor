"""Unit: the agent cannot push; the orchestrator owns GitHub mutations."""
import subprocess
from pathlib import Path

from contributor.execution.git import block_push, ensure_scratch_dir


def _init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True)
    (path / "a.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return path


def test_pre_push_hook_blocks_push(tmp_path: Path):
    ws = _init(tmp_path / "repo")
    block_push(ws)
    hook = ws / ".git" / "hooks" / "pre-push"
    assert hook.exists()
    assert hook.stat().st_mode & 0o111
    assert "orchestrator" in hook.read_text()


def test_scratch_dir_is_gitignored(tmp_path: Path):
    ws = _init(tmp_path / "repo")
    ensure_scratch_dir(ws)
    (ws / ".contributor-scratch" / "junk.txt").write_text("temp")
    status = subprocess.run(
        ["git", "-C", str(ws), "status", "--porcelain"], capture_output=True, text=True
    )
    assert ".contributor-scratch" not in status.stdout
