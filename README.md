# Autonomous Contributor

Production-oriented multi-agent GitHub contributor:

- **LangGraph** controls the workflow (persisted, restartable state machine).
- **OpenCode CLI** does the actual coding.
- **Docker** isolates every repository.
- **Deterministic code** decides tests/git/CI; LLMs do reasoning + codegen only.
- **Bounded loops**, concurrency caps, human escalation as a first-class state.

## Quickstart

```bash
cd autonomous-contributor
uv sync
cp .env.example .env   # add GITHUB_TOKEN
docker build -f docker/sandbox/Dockerfile -t contributor-sandbox:latest .
contributor issue OWNER/REPO#123
```

## Workflow

```
DISCOVER → TRIAGE → ENVIRONMENT_DISCOVERY → PLAN → PREPARE_WORKSPACE → IMPLEMENT
→ TEST (targeted) → REVIEW → push gate → COMMIT → PUSH → verify remote ref
→ create/reuse PR → CI monitor
   ├─ CI pass / no checks → MERGE_READY (STOP; never auto-merge)
   ├─ repairable CI failure → CI_REPAIRING → TEST → REVIEW → push gate → repair
   │                          commit → PUSH → update PR → CI monitor …
   └─ infra/env failure → ESCALATE / ENVIRONMENT_FAILURE · exhausted → CI_REPAIR_EXHAUSTED
```

Bounded `DEBUG`/repair loops, human escalation, and resumable persisted state.

## Modes

- **benchmark** — implement/test/review only; cannot mutate GitHub.
- **live branch** — `contributor live OWNER/REPO#N --push-repo FORK/REPO` pushes a branch.
- **live PR** — add `--pr` to open a PR and monitor CI; bounded CI repair; never merges.

```bash
contributor live OWNER/REPO#123 --pr --push-repo FORK/REPO
contributor metrics            # PR/CI lifecycle rates
contributor resume JOB_ID      # continue an interrupted job
```


## Layout

- `src/contributor/graph/` — LangGraph workflow, nodes, routing
- `src/contributor/agents/` — triage/planner/implementer/debugger/reviewer
- `src/contributor/opencode/` — CLI runner + prompts
- `src/contributor/execution/` — git + multi-ecosystem tests
- `src/contributor/sandbox/` — Docker isolation + limits
- `src/contributor/github/` — API client, issues, PRs, CI
- `src/contributor/persistence/` — SQLite (Postgres-compatible schema)
- `docs/` — architecture, security, operations

## Safety highlights

No host FS/SSH/credentials in containers, CPU/mem/pid/network caps, disposable
workspaces, orchestrator-owned git lifecycle, programmatic safety blocklist.
See `docs/security.md`.
