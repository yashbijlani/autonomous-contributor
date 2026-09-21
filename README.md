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

DISCOVER → TRIAGE → PLAN → PREPARE_WORKSPACE → IMPLEMENT → TEST → REVIEW
→ CREATE_PR → PR_CI_CHECK → WAIT_FOR_REVIEW, with bounded DEBUG/repair loops
and HUMAN_REQUIRED → ESCALATE.

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
