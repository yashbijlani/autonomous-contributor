# Architecture

```
GitHub issue → LangGraph (orchestrator) → OpenCode (worker) → Docker sandbox → GitHub PR
```

## Components

- **LangGraph (`src/contributor/graph/`)** — owns the state machine. Nodes are thin;
  routing (`routing.py`) enforces `MAX_*` caps. Every node persists via `JobRepository`.
- **Agents (`src/contributor/agents/`)** — triage (safety gates), planner (read-only),
  implementer/debugger (OpenCode prompts), reviewer (independent diff analysis).
- **OpenCode (`src/contributor/opencode/`)** — `OpenCodeRunner` subprocess wrapper with
  timeout, cancellation, exit-code capture. Prompts forbid commit/push.
- **Execution (`src/contributor/execution/`)** — `git.py` (orchestrator-owned git),
  `tests.py` (multi-ecosystem detection, allowlist, exit-code verdicts).
- **Sandbox (`src/contributor/sandbox/`)** — Docker `--cpus/--memory/--pids-limit`,
  `--network none` option, workspace bind-mount only. Local fallback for dev/tests.
- **GitHub (`src/contributor/github/`)** — httpx REST client + `RateLimitedError`;
  swappable via `GitHubClientProtocol`. CI interpreted deterministically.
- **Persistence (`src/contributor/persistence/`)** — SQLite now, portable schema for
  PostgreSQL later. Jobs stored as JSON + queryable columns; events appended.
- **Discovery (`src/contributor/discovery/`)** — read-only search → candidates.
- **Observability (`src/contributor/observability/`)** — canonical event names,
  structured logging, per-job event history.
- **MCP (`src/contributor/mcp.py`)** — optional tool policies per agent (not orchestration).

## Model addressing & provider failures

- `--model <provider>/<model>` selects the model; `--variant <high|xhigh>`
  selects reasoning effort (`opencode/client.py:resolve_spec`). Tier aliases are
  never passed as model IDs.
- `OpenCodeRunner` preserves the raw stderr and normalizes it into a stable
  taxonomy (`opencode/runner.py:classify_error`): AUTH_FAILURE, MODEL_NOT_FOUND,
  VARIANT_UNSUPPORTED, PROVIDER_NETWORK_ERROR, PROVIDER_RATE_LIMIT,
  PROVIDER_SERVER_ERROR, PROVIDER_TIMEOUT, MALFORMED_REQUEST, PERMISSION_REJECTED…
- Provider-blocking categories short-circuit to `BLOCKED_PROVIDER`
  (`graph/routing.py:after_implement`). Missing-host-tool test failures short-
  circuit to `REPOSITORY_ENVIRONMENT_INCOMPATIBLE` (`after_test`).
- `opencode/preflight.py` runs binary → version → auth/model → tiny inference
  before any clone. `opencode/doctor.py` backs `contributor doctor`.

## State flow

DISCOVER → TRIAGE → ENVIRONMENT_DISCOVERY → PLAN → PREPARE_WORKSPACE → IMPLEMENT
→ TEST → REVIEW → APPROVED → CREATE_PR → PR_CI_CHECK → WAIT_FOR_REVIEW → DONE
→ CHANGES_REQUIRED → IMPLEMENT · TEST_FAILURE → DEBUG · HUMAN_REQUIRED → ESCALATE
→ ENVIRONMENT_FAILURE (CASE E) · REPOSITORY_ENVIRONMENT_INCOMPATIBLE (CASE B)
→ BLOCKED_PROVIDER (CASE A)

## Model routing & environment

- `agents/model_router.py` maps task type + difficulty + prior failures to a tier
  (fast/cheap/strong/max), then to `ModelSpec(provider, model, variant)`.
- `agents/environment.py` + `sandbox/strategy.py` inspect the repo (read-only)
  and choose an execution strategy; never weaken isolation to pass tests.
- `persistence/model_health.py` caches per-model preflight health (TTL);
  `persistence/usage.py` records model accounting.

See `src/contributor/graph/workflow.py` for the compiled `StateGraph`.
