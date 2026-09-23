# Architecture

Autonomous contributor: a **hierarchical multi-agent system with deterministic
orchestration** (not a "decentralized swarm"; that term, if ever used, is informal).
The LLM reasons and writes code; deterministic code owns every state transition,
exit-code verdict, git fact, and GitHub mutation.

```
GitHub issue
    │
    ▼
┌────────────────────────────── HOST / ORCHESTRATOR ──────────────────────────────┐
│  LangGraph workflow (persisted, resumable)     GitHub client + credentials       │
│  model routing · resource governance            PR creation · CI monitoring       │
│  deterministic push gate · git lifecycle        CI failure classification         │
│  persistent state (SQLite)                      CI repair bound                   │
└───────────┬───────────────────────────────────────────────────────┬─────────────┘
            │ mounts only repo + env cache (no token/SSH/socket)     │ HTTPS (orchestrator only)
            ▼                                                        ▼
┌────────────────────────── ISOLATED SANDBOX ───────────────────┐   GitHub API
│  OpenCode · repository · toolchain · dependencies · tests     │   (PR + checks)
│  cannot push · cannot reach the GitHub token                   │
└───────────────────────────────────────────────────────────────┘
```

## Lifecycle

```
DISCOVER → TRIAGE → ENVIRONMENT_DISCOVERY → PLAN → PREPARE_WORKSPACE
→ IMPLEMENT → TEST (targeted) → REVIEW → push gate → COMMIT → PUSH
→ verify remote ref → create/reuse PR → CI monitor
   ├─ CI pass / no checks → MERGE_READY (STOP; never auto-merge)
   ├─ repairable CI failure → CI_REPAIRING → TEST → REVIEW → push gate
   │                          → repair commit → PUSH → update PR → CI monitor …
   ├─ infra/env/fault failure → ESCALATE / ENVIRONMENT_FAILURE (no code churn)
   └─ repair budget exhausted → CI_REPAIR_EXHAUSTED
```

Local test/review failures use the existing bounded `DEBUG` loop. CI failures
use the separate bounded `ci_repair_attempt` budget (`MAX_CI_REPAIR_CYCLES`).

## Components

- **LangGraph (`src/contributor/graph/`)** — owns the state machine. Nodes are thin;
  routing (`routing.py`) enforces `MAX_*` caps. Every node persists via `JobRepository`.
- **Agents (`src/contributor/agents/`)** — triage (safety gates), planner (read-only),
  implementer/debugger/ci_repair (OpenCode prompts), reviewer (independent diff analysis).
- **OpenCode (`src/contributor/opencode/`)** — `OpenCodeRunner` subprocess wrapper with
  timeout, cancellation, exit-code capture. Prompts forbid commit/push.
- **Execution (`src/contributor/execution/`)** — `git.py` (orchestrator-owned git,
  remote-ref verification), `tests.py` (multi-ecosystem detection, targeted tests,
  allowlist, exit-code verdicts), `push_gate.py` (deterministic pre-push gate).
- **Sandbox (`src/contributor/sandbox/`)** — Docker `--cpus/--memory/--pids-limit`,
  `--network none` option, workspace bind-mount only. Local fallback for dev/tests.
- **GitHub (`src/contributor/github/`)** — httpx REST client + `RateLimitedError`;
  `pull_requests.py` (dynamic head/base resolution, reuse, templates),
  `ci.py` (check normalization, bounded polling, failure classification), CI logs.
- **Persistence (`src/contributor/persistence/`)** — SQLite now, portable schema for
  PostgreSQL later. Jobs stored as JSON + queryable columns; events appended.
- **Observability (`src/contributor/observability/`)** — canonical event names,
  structured logging, per-job event history, PR/CI metrics.
- **Discovery (`src/contributor/discovery/`)** — read-only search → candidates.

## PR creation and fork workflow

The PR is created **only after** the branch push is verified on the remote
(`git ls-remote`; `REMOTE_REF_VERIFIED`). Head/base are resolved dynamically:

- base is always the **issue repository** and its configured default branch;
- branch pushed to a **fork** → head is `<fork-owner>:<branch>` (the fork owner
  comes from the configured push target / authenticated login, never hardcoded);
- branch pushed **directly** to the base repo → head is `<branch>`.

An existing PR for the branch is reused (and its body refreshed) instead of
opening a duplicate. The repository's PR template, when present, is used as the
body base and never overwritten.

## CI monitoring and repair

`ci.py` normalizes GitHub statuses and check-runs into `{name, workflow, status,
conclusion, url, started_at, completed_at, duration_s, app, required, state}`,
polls with a bounded interval/wait (`CI_POLL_INTERVAL_S`, `CI_MAX_WAIT_S`), and
classifies failures deterministically:

`CODE_FAILURE`, `TEST_FAILURE`, `BUILD_FAILURE` → repairable;
`DEPENDENCY_FAILURE`, `ENVIRONMENT_FAILURE` → terminal environment state;
`RESOURCE_FAILURE`, `NETWORK_FAILURE`, `PERMISSION_FAILURE`,
`CI_INFRASTRUCTURE_FAILURE`, `FLAKY_FAILURE` → never sent to the coding agent
(bounded, code-free CI retry may be attempted first for flaky/infra).

Only repairable failures consume the bounded repair budget. Repair runs in the
same repository/sandbox, reproduces narrowly, then flows through the existing
TEST → REVIEW → push-gate → PUSH → update-PR path. Repair commits preserve
history (parent/new SHA, reason, tests, review recorded in `repair_history`).

## Resume

Every transition is persisted. `contributor resume <job>` maps the persisted
`current_state` to the correct graph entry (`resume_entry`), so an interrupted
run continues at PR creation, CI monitoring, or repair — never re-implementing
from scratch and never opening a second PR.

## Model addressing & provider failures

- `--model <provider>/<model>` selects the model; `--variant <high|xhigh>`
  selects reasoning effort. Tier aliases are never passed as model IDs.
- `OpenCodeRunner` normalizes raw stderr into a stable taxonomy
  (`opencode/runner.py:classify_error`).
- Provider-blocking categories short-circuit to `BLOCKED_PROVIDER`
  (`graph/routing.py:after_implement`).

See `src/contributor/graph/workflow.py` for the compiled `StateGraph`.

