# Security model

1. **Untrusted code containment** — every repository checkout lives under
   `.workspaces/job-<id>/` and executes only inside Docker (`sandbox/docker.py`).
   Bind-mount: workspace → `/workspace` only. No host FS, no `~/.ssh`, no Docker socket.
2. **Secret hygiene** — `GITHUB_TOKEN` and `*_KEY` env vars are stripped before entering
   the container (`sanitized_env` + `DockerSandbox.exec` allowlist). Git push auth happens
   on the host via remote URL, never inside the container.
3. **Network policy** — `SANDBOX_NETWORK=none` disables egress; default bridge allows
   package installs. `SANDBOX_ALLOW_NETWORK=false` forces `--network none`.
4. **Resource caps** — `--cpus`, `--memory`, `--pids-limit`, per-command timeouts,
   `JOB_TIMEOUT_S`, `MAX_CONCURRENT_JOBS`. Containers are `--rm` (disposable).
5. **Supply-chain caution** — repo build scripts (`Makefile`, `setup.py`, hooks) are
   treated as untrusted; only orchestrator allowlisted test commands run, and
   destructive patterns (`rm -rf /`, `curl|sh`) are rejected (`execution/tests.py`).
6. **Git safety** — orchestrator never commits to `main`/`master`, never pushes those
   branches; unique `contrib/issue-<n>-<job>` branches only.
7. **Triage blocklist** — credential/secret/destructive-infra/product-decision issues are
   rejected or escalated programmatically, not by LLM judgement.
8. **Deterministic verdicts** — tests (exit codes), git (status/diff), CI (check-runs)
   are read programmatically. LLMs never decide these facts.
9. **Auditability** — every transition emits an event persisted in SQLite + logs.
