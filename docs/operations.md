# Operations

## Setup

```bash
git clone <this-repo> && cd autonomous-contributor
uv sync            # or: pip install -e ".[dev]"
cp .env.example .env
# edit GITHUB_TOKEN in .env
```

Prereqs: Python 3.12+, Docker, `opencode` CLI on PATH, `GITHUB_TOKEN` with `repo` scope.

Build sandbox image:

```bash
docker build -f docker/sandbox/Dockerfile -t contributor-sandbox:latest .
```

## Model configuration (important)

OpenCode addresses models as `provider/model`; reasoning effort is a separate
`--variant` flag. Tier names (`fast`, `cheap`, `strong`, `max`, `high`, `xhigh`)
are **never** valid model IDs.

```bash
OPENCODE_MODEL_FAST=opencode-go/deepseek-v4.1-flash
OPENCODE_MODEL_CHEAP=opencode-go/deepseek-v4.1-flash
OPENCODE_MODEL_STRONG=opencode-go/deepseek-v4.1-flash
OPENCODE_MODEL_MAX=opencode-go/deepseek-v4.1-flash
OPENCODE_VARIANT_STRONG=high
OPENCODE_VARIANT_MAX=xhigh
MODEL_ROUTER_POLICY=            # optional JSON, e.g. {"implement":"max"}
```

Discover real IDs with `opencode models`. The `ModelRouter` maps each task to a
tier (triage/env/plan → cheap; implementation/debug/review → strong, escalating
to max on difficulty or repeated failure) and resolves it to `(model, variant)`.
Provider/model/variant are never collapsed.

## Usage

```bash
contributor doctor                      # diagnose OpenCode integration
contributor doctor --json               # machine-readable
contributor issue OWNER/REPO#123        # full workflow (default)
contributor issue OWNER/REPO#123 --dry-run
contributor run OWNER/REPO#123
contributor run OWNER/REPO#123 --push --pr --ci --push-repo FORK/REPO
contributor live OWNER/REPO#123 --push-repo FORK/REPO            # push only
contributor live OWNER/REPO#123 --pr --push-repo FORK/REPO       # push + PR + CI
contributor discover --language python --label "good first issue"
contributor status JOB_ID
contributor logs JOB_ID
contributor metrics                      # aggregate PR/CI lifecycle metrics
contributor metrics --job JOB_ID
contributor resume JOB_ID                # continue from persisted state
contributor cancel JOB_ID
contributor autonomous --once
```

## Modes

| Mode | push | create_pr | ci_monitor | notes |
| --- | --- | --- | --- | --- |
| `benchmark` | no | no | no | cannot reach any GitHub mutation code |
| `live branch` | yes | no | no | `contributor live …` (no `--pr`) |
| `live PR` | yes | yes | yes | `contributor live … --pr` |
| `live PR + repair` | yes | yes | yes | repair is enabled by live PR mode, bounded |

Auto-merge is **never** enabled in this milestone. The workflow stops at
`MERGE_READY`; a human performs the merge.

## PR / CI configuration

```bash
MAX_CI_REPAIR_CYCLES=3      # bounded CI repair attempts
CI_VERIFY_REMOTE=true       # verify the pushed ref before creating a PR
CI_POLL_INTERVAL_S=20       # host-side poll interval
CI_MAX_WAIT_S=1800          # maximum CI wait before classifying a timeout
CI_SETTLE_S=60              # wait for checks to appear before "no checks"
CI_FLAKY_RETRIES=1          # bounded code-free retry for flaky/infra CI
CI_LOG_BUDGET_CHARS=12000   # cap on CI log text sent to the model
PR_USE_TEMPLATE=true        # use the repo PR template when present
```

## CI failure classification

Only failures plausibly caused by the contribution are repaired:

| Class | Action |
| --- | --- |
| `CODE_FAILURE`, `TEST_FAILURE`, `BUILD_FAILURE` | bounded CI repair |
| `UNKNOWN` | bounded CI repair (conservative) |
| `DEPENDENCY_FAILURE`, `ENVIRONMENT_FAILURE` | terminal environment state |
| `RESOURCE_FAILURE`, `NETWORK_FAILURE`, `PERMISSION_FAILURE` | escalate |
| `CI_INFRASTRUCTURE_FAILURE`, `FLAKY_FAILURE` | bounded CI retry, then escalate |

The classification is deterministic and never decided by the LLM.

`issue`/`run`/`autonomous` preflight **every model tier** (binary, version, auth,
model listed, tiny inference) before cloning or spending tokens. Results are
cached (`MODEL_HEALTH_TTL_S`) so jobs don't re-probe. If at least one tier works
the job proceeds; only if none work is it marked `BLOCKED_PROVIDER`.

`contributor doctor` reports each tier (model, variant, auth, inference,
latency), plus network and sandbox. `contributor usage` shows model accounting.

## Failure classification

| Case | Meaning | State |
| --- | --- | --- |
| A | OpenCode cannot infer (auth/model/network/config) | `BLOCKED_PROVIDER` |
| B | Repo cannot run in the available sandbox (host/systemd) | `REPOSITORY_ENVIRONMENT_INCOMPATIBLE` |
| C | OpenCode works, tests run, implementation fails | `DEBUG` → bounded repair |
| D | Tests fail because of the implementation | `DEBUG` |
| E | Tests fail because dependencies/environment are missing | `ENVIRONMENT_FAILURE` |
| — | CI infrastructure failure (runner/cancel) | `ESCALATE` (no debug loop) |

Environment discovery runs before planning and records languages, tools, test
commands, `container_compatible`, and an `EnvironmentStrategy`
(`standard_docker` / `custom_docker` / `devcontainer` / `host_required` /
`incompatible`). Set `ENV_HARD_GATE=true` to escalate host-required repos before
any implementation; otherwise they proceed and are classified on failure.

## Resume after crash

Jobs persist after every node (`JobRepository.save_incremental`). After a restart:

```bash
contributor resume <JOB_ID>
```

## Troubleshooting

- `opencode binary not found` → install opencode or set `OPENCODE_BINARY`.
- `Unexpected server error` with a model id that has no `/` → you passed a tier
  alias (e.g. `--model high`). Set `OPENCODE_MODEL_*` to full `provider/model`
  IDs and put the effort in `OPENCODE_VARIANT_*`.
- `[user_blocked]` / provider restriction → upstream account/model issue; pick
  another model via `OPENCODE_MODEL_*` (verify with `opencode models`).
- `Docker required but unavailable` → unset `REQUIRE_DOCKER` for dev, or start Docker
  and build the sandbox image.
- `GitHub rate limited` → back off; client raises `RateLimitedError` with `retry_after`.
- `clone failed` → check token scope / repo visibility.
- `No changes to create PR from` → OpenCode produced no diff; check logs for prompt errors.
- `GitHub API 403 … /pulls` on PR creation → the configured token cannot write pull
  requests for the base repository. Use a credential with `repo` scope (classic PAT or
  `gh auth token`) for live PR mode. The push to the fork can succeed with a narrower
  token while PR creation still requires PR-write access to the base repository.
- Tests report `(no test command)` → ecosystem undetected; planner can suggest an
  allowlisted command via `plan.commands_to_run`.
