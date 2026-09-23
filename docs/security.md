# Security model

1. **Untrusted code containment** — every repository checkout lives under
   `.workspaces/job-<id>/` and executes only inside Docker (`sandbox/docker.py`).
   Bind-mount: workspace → `/workspace/repo` plus a dedicated env cache; no host
   FS, no `~/.ssh`, no Docker socket.
2. **Secret hygiene** — `GITHUB_TOKEN` and `*_KEY` env vars are stripped before entering
   the container (`sanitized_env` + `DockerSandbox.exec` allowlist). The sandbox never
   receives the GitHub token; PR creation, CI polling and every other GitHub mutation
   happen on the host orchestrator.
3. **Network policy** — `SANDBOX_NETWORK=none` disables egress; default bridge allows
   package installs. `SANDBOX_ALLOW_NETWORK=false` forces `--network none`.
4. **Resource caps** — `--cpus`, `--memory`, `--pids-limit`, per-command timeouts,
   `JOB_TIMEOUT_S`, `MAX_CONCURRENT_JOBS`. Containers are `--rm` (disposable).
5. **Supply-chain caution** — repo build scripts (`Makefile`, `setup.py`, hooks) are
   treated as untrusted; only orchestrator allowlisted test commands run, and
   destructive patterns (`rm -rf /`, `curl|sh`) are rejected (`execution/tests.py`).
6. **Git safety** — the orchestrator never commits to `main`/`master` and never pushes
   those branches (`push_gate.py`, `execution/git.py`); unique
   `contrib/issue-<n>-<job>` branches only. Pushes use `--no-verify` from the host so
   the agent-facing pre-push hook (`block_push`) stays in force for the agent.
7. **Remote-ref verification** — a PR is created only after `git ls-remote` confirms
   the pushed branch exists on the remote at the expected commit
   (`REMOTE_REF_VERIFIED`). No PR without a verified push.
8. **PR target determinism** — the PR base is always the issue repository and its
   default branch; the head owner is derived from the configured push target / the
   authenticated login, never hardcoded. Existing PRs are reused, never duplicated.
9. **Bounded CI repair** — only deterministic, contribution-caused failure classes
   (`CODE/TEST/BUILD/UNKNOWN`) may consume the bounded repair budget. Resource,
   network, permission, CI-infrastructure and flaky failures are never handed to the
   coding agent. No force pushes, no auto-merge.
10. **Triage blocklist** — credential/secret/destructive-infra/product-decision issues
    are rejected or escalated programmatically, not by LLM judgement.
11. **Deterministic verdicts** — tests (exit codes), git (status/diff), CI (check-runs)
    are read programmatically. LLMs never decide these facts.
12. **Auditability** — every transition emits an event persisted in SQLite + logs;
    repairs record parent/new SHA, reason, tests and review verdict.
