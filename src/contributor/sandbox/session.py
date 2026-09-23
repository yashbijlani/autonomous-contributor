"""Long-lived sandbox session: one container per job shared by OpenCode,
dependency bootstrap, and tests.

Security model:
- only the job workspace and a controlled host cache dir are mounted
- provider auth is mounted READ-ONLY; it is never copied into an image
- no host SSH keys / dotfiles / Docker socket are mounted
- runs as the invoking host uid/gid so bind-mounted files are writable
- the orchestrator (host) keeps exclusive control of git push / GitHub
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from contributor.config import Settings
from contributor.observability.logging import get_logger
from contributor.sandbox.docker import ExecResult
from contributor.sandbox.resources import ResourceProfile, parse_memory

log = get_logger("contributor.sandbox.session")

_MAX_CAPTURE = 2_000_000


def _run(argv: list[str], timeout: int) -> tuple[int, str, str, float, bool]:
    start = time.monotonic()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        dur = time.monotonic() - start
        return p.returncode, p.stdout[-_MAX_CAPTURE:], p.stderr[-_MAX_CAPTURE:], dur, False
    except subprocess.TimeoutExpired as e:
        dur = time.monotonic() - start
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else str(e.stdout or "")
        err = (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else str(e.stderr or "")
        return 124, out[-_MAX_CAPTURE:], (err + f"\n[TIMEOUT after {timeout}s]")[-_MAX_CAPTURE:], dur, True
    except FileNotFoundError as e:
        return 127, "", str(e), 0.0, False


class SandboxSession:
    def __init__(
        self,
        settings: Settings,
        *,
        name: str,
        workspace: Path,
        env_dir: Path,
        profile: ResourceProfile,
        image: str | None = None,
    ):
        self.settings = settings
        self.name = name
        self.workspace = Path(workspace)
        self.env_dir = Path(env_dir)
        self.profile = profile
        self.image = image or settings.sandbox_image
        self.workspace_mount = settings.sandbox_workspace_mount
        self.env_mount = settings.sandbox_env_mount
        self._started = False

    # ---- host-side preparation ----
    def _opencode_binary(self) -> str:
        if self.settings.opencode_host_binary:
            return str(Path(self.settings.opencode_host_binary).expanduser())
        return shutil.which(self.settings.opencode_binary) or ""

    def _auth_file(self) -> str:
        return str(Path(self.settings.opencode_auth_file).expanduser())

    def _config_file(self) -> str:
        return str(Path(self.settings.opencode_config_file).expanduser())

    def _prepare_env_dir(self) -> None:
        home = self.env_dir / "home"
        for sub in (
            home / ".local/share/opencode",
            home / ".config/opencode",
            self.env_dir / "cargo",
            self.env_dir / "rustup",
            self.env_dir / "go",
        ):
            sub.mkdir(parents=True, exist_ok=True)

    def _mounts(self) -> list[str]:
        mounts = [
            "-v", f"{self.workspace.resolve()}:{self.workspace_mount}:rw",
            "-v", f"{self.env_dir.resolve()}:{self.env_mount}:rw",
        ]
        binary = self._opencode_binary()
        if binary and Path(binary).exists():
            mounts += ["-v", f"{binary}:/usr/local/bin/opencode:ro"]
        auth = self._auth_file()
        if auth and Path(auth).exists():
            mounts += ["-v", f"{auth}:{self.env_mount}/home/.local/share/opencode/auth.json:ro"]
        cfg = self._config_file()
        if cfg and Path(cfg).exists():
            mounts += ["-v", f"{cfg}:{self.env_mount}/home/.config/opencode/opencode.json:ro"]
        return mounts

    def _env_args(self) -> list[str]:
        env = {
            "HOME": f"{self.env_mount}/home",
            "XDG_DATA_HOME": f"{self.env_mount}/home/.local/share",
            "XDG_CONFIG_HOME": f"{self.env_mount}/home/.config",
            "CARGO_HOME": f"{self.env_mount}/cargo",
            "RUSTUP_HOME": f"{self.env_mount}/rustup",
            # Persist build artifacts across the implementation and test phases
            # (and across jobs for the same repo/toolchain) to avoid rebuilding.
            "CARGO_TARGET_DIR": f"{self.env_mount}/cargo-target",
            "GOCACHE": f"{self.env_mount}/go-cache",
            "GOPATH": f"{self.env_mount}/go",
            "PATH": (
                f"{self.env_mount}/cargo/bin:{self.env_mount}/go/bin:"
                "/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            ),
            "CI": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
        }
        args: list[str] = []
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        return args

    def _network_args(self) -> list[str]:
        if not self.settings.sandbox_allow_network or self.settings.sandbox_network == "none":
            return ["--network", "none"]
        if self.settings.sandbox_network and self.settings.sandbox_network != "bridge":
            return ["--network", self.settings.sandbox_network]
        return []

    def _user(self) -> str:
        return f"{os.getuid()}:{os.getgid()}"

    def start(self) -> None:
        if self._started:
            return
        self._prepare_env_dir()
        mem = parse_memory(self.profile.memory)
        argv = [
            "docker", "run", "-d", "--name", self.name,
            "--user", self._user(),
            "--init",
            "--cpus", str(self.profile.cpus),
            "--memory", self.profile.memory,
            # permit swap up to 2x memory so heavy builds can spill
            "--memory-swap", f"{max(1, mem * 2 // 1024**2)}m",
            "--pids-limit", str(self.settings.sandbox_pids_limit),
            *self._network_args(),
            *self._env_args(),
            *self._mounts(),
            "-w", self.workspace_mount,
            self.image, "sleep", "infinity",
        ]
        code, out, err, _, _ = _run(argv, timeout=self.settings.docker_timeout_s)
        if code != 0:
            raise RuntimeError(f"failed to start sandbox session: {err[-800:]}")
        self._started = True
        log.info("sandbox session started %s image=%s profile=%s", self.name, self.image, self.profile.name)

    def exec_shell(
        self,
        command: str,
        *,
        timeout: int = 600,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        argv = ["sh", "-c", command]
        return self._exec(argv, timeout=timeout, workdir=workdir, env=env, command=command)

    def exec_argv(
        self,
        argv: list[str],
        *,
        timeout: int = 600,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        return self._exec(list(argv), timeout=timeout, workdir=workdir, env=env, command=" ".join(argv[:4]))

    def _exec(
        self,
        argv: list[str],
        *,
        timeout: int,
        workdir: str | None,
        env: dict[str, str] | None,
        command: str,
    ) -> ExecResult:
        if not self._started:
            raise RuntimeError("sandbox session not started")
        docker_argv = ["docker", "exec", "-i"]
        if workdir:
            docker_argv += ["-w", workdir]
        for k, v in (env or {}).items():
            docker_argv += ["-e", f"{k}={v}"]
        docker_argv += [self.name, *argv]
        code, out, err, dur, timed_out = _run(docker_argv, timeout=timeout)
        return ExecResult(command=command, exit_code=code, stdout=out, stderr=err, duration_s=dur, timed_out=timed_out)

    def stop(self) -> None:
        if not self._started:
            return
        _run(["docker", "rm", "-f", self.name], timeout=60)
        self._started = False
        log.info("sandbox session stopped %s", self.name)


class SandboxSessionRegistry:
    """Per-context registry of job sessions (one container per job)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._sessions: dict[str, SandboxSession] = {}

    def get(self, job_id: str) -> SandboxSession | None:
        return self._sessions.get(job_id)

    def get_or_create(
        self,
        job_id: str,
        *,
        workspace: Path,
        env_dir: Path,
        profile: ResourceProfile,
        image: str | None = None,
    ) -> SandboxSession:
        existing = self._sessions.get(job_id)
        if existing is not None:
            return existing
        session = SandboxSession(
            self.settings,
            name=f"contributor-{job_id[:12]}",
            workspace=workspace,
            env_dir=env_dir,
            profile=profile,
            image=image,
        )
        self._sessions[job_id] = session
        return session

    def stop(self, job_id: str) -> None:
        session = self._sessions.pop(job_id, None)
        if session is not None:
            session.stop()

    def stop_all(self) -> None:
        for job_id in list(self._sessions):
            self.stop(job_id)
