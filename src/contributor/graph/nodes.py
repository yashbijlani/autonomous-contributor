"""Workflow nodes: each node loads JobState, does deterministic work, persists, returns patch.

All LLM work happens in agents/ (triage/planner/opencode/reviewer). Nodes never
let an LLM decide exit codes, git status, or CI state — those come from
execution/ + github/ programmatic results.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contributor.agents.debugger import run_debug
from contributor.agents.implementer import run_implementation
from contributor.agents.planner import build_plan
from contributor.agents.reviewer import review_change
from contributor.agents.triage import triage_issue
from contributor.config import Settings
from contributor.execution.git import (
    changed_files,
    checkout_new_branch,
    clone_repo,
    commit_all,
    diff_full,
    has_changes,
    make_branch_name,
    push_branch,
)
from contributor.execution.tests import TestRunner
from contributor.github.ci import fetch_ci, interpret_ci
from contributor.github.client import GitHubClient
from contributor.github.issues import fetch_issue, normalize_issue
from contributor.github.pull_requests import publish_pr, render_pr_body, render_pr_title, update_pr_body
from contributor.models.state import (
    CIResult,
    ImplementationPlan,
    JobState,
    JobStatus,
    ReviewResult,
    TestResult,
    TriageResult,
)
from contributor.observability import events
from contributor.observability.events import emit
from contributor.opencode.runner import OpenCodeRunner
from contributor.persistence.repositories import JobRepository
from contributor.sandbox.manager import SandboxManager


@dataclass
class WorkflowContext:
    settings: Settings
    db: Any  # Database
    github: GitHubClient
    sandbox: SandboxManager
    runner: OpenCodeRunner
    # Benchmark mode: the graph terminates at review approval WITHOUT pushing a
    # branch or creating a PR. Default False (normal contributor behavior).
    benchmark_mode: bool = False
    # Registry of in-sandbox sessions (OpenCode + deps + tests share one env).
    sessions: Any = None
    # Live contribution mode + orchestrator-owned push (never set by benchmark).
    execution_mode: str = "benchmark"  # benchmark | live
    allow_push: bool = False
    allow_create_pr: bool = False
    allow_comments: bool = False
    allow_labels: bool = False
    # Live PR mode: monitor GitHub checks after the PR is opened.
    ci_monitor: bool = False
    # Recorded only; automatic merging is never performed in this milestone.
    auto_merge: bool = False
    push_repo: str = ""  # OWNER/REPO target; empty => issue repository
    push_remote: str = "origin"

    @property
    def repo(self) -> JobRepository:
        from contributor.persistence.repositories import JobRepository as JR

        return JR(self.db)

    @property
    def health(self):
        from contributor.persistence.model_health import ModelHealthStore

        return ModelHealthStore(self.db, ttl_s=self.settings.model_health_ttl_s)

    @property
    def router(self):
        from contributor.agents.model_router import ModelRouter

        return ModelRouter(self.settings)


def _runner_for(st: JobState, ctx: WorkflowContext):
    """Return the in-sandbox runner when a session exists, else the host runner."""
    if ctx.sessions is not None:
        session = ctx.sessions.get(st.job_id)
        if session is not None:
            from contributor.opencode.runner import SandboxOpenCodeRunner

            return SandboxOpenCodeRunner(session, ctx.settings)
    return ctx.runner


def _load(ctx: WorkflowContext, s: dict) -> JobState:
    st = ctx.repo.get(s["job_id"])
    if st is None:
        # first node: construct from graph input
        st = JobState(job_id=s["job_id"], repository=s.get("repository", ""), issue_number=s.get("issue_number", 0))
        for k, v in s.items():
            if hasattr(st, k):
                try:
                    setattr(st, k, v)
                except Exception:
                    pass
    return st


def _save(ctx: WorkflowContext, st: JobState) -> None:
    ctx.repo.save_incremental(st)


def _patch(st: JobState) -> dict:
    return st.model_dump(mode="python")


# ---- nodes ----
def node_fetch_issue(state: dict, ctx: WorkflowContext) -> dict:
    st = _load(ctx, state)
    st.current_state = JobStatus.DISCOVER
    emit(st, events.ISSUE_FETCHED, f"Fetching {st.repository}#{st.issue_number}")
    try:
        owner, repo = st.repository.split("/", 1)
        data = ctx.github.get_issue(owner, repo, st.issue_number)
    except Exception as e2:
        st.errors.append(f"issue fetch failed: {e2}")
        emit(st, events.JOB_FAILED, str(e2))
        st.current_state = JobStatus.FAILED
        st.done = True
        _save(ctx, st)
        return _patch(st)
    from contributor.github.issues import normalize_issue as norm

    title, body, meta = norm(data)
    st.issue_title = title
    st.issue_body = body
    st.issue_metadata = meta
    try:
        r = ctx.github.get_repo(owner, repo)
        st.issue_metadata["repo_stars"] = r.get("stargazers_count")
        st.issue_metadata["repo_language"] = r.get("language")
        st.issue_metadata["clone_url"] = r.get("clone_url")
        st.issue_metadata["default_branch"] = r.get("default_branch", "main")
    except Exception as e:
        st.errors.append(f"repo metadata failed: {e}")
    st.current_state = JobStatus.TRIAGE
    emit(st, events.JOB_CREATED, f"Issue fetched: {title[:100]}")
    _save(ctx, st)
    return _patch(st)


def node_triage(state: dict, ctx: WorkflowContext) -> dict:
    st = _load(ctx, state)
    st.current_state = JobStatus.TRIAGE
    emit(st, events.TRIAGE_STARTED, "Triage started", agent="triage")
    # best-effort repo docs for context
    readme = contributing = ""
    try:
        owner, repo = st.repository.split("/", 1)
        readme = ctx.github.get_file(owner, repo, "README.md") or ""
        contributing = ctx.github.get_file(owner, repo, "CONTRIBUTING.md") or ""
    except Exception:
        pass
    labels = st.issue_metadata.get("labels", []) if isinstance(st.issue_metadata.get("labels"), list) else []
    tr = triage_issue(st.issue_title, st.issue_body, labels=labels, repo_readme=readme[:2000], repo_contributing=contributing[:2000])
    st.triage_result = tr
    try:
        from contributor.agents.model_router import TASK_TRIAGE

        spec = ctx.router.select(TASK_TRIAGE, tr.difficulty, 0)
        st.model_selections["triage"] = spec.label()
        emit(st, events.MODEL_SELECTED, f"triage -> {spec.label()}", agent="triage",
             data={"task": "triage", "model": spec.model, "variant": spec.variant})
    except Exception:
        st.model_selections["triage"] = ""
    emit(st, events.TRIAGE_COMPLETED, f"Triage: {tr.decision.value} ({tr.reason[:200]})", agent="triage", data={"decision": tr.decision.value})
    if tr.decision.value == "accept":
        st.current_state = JobStatus.ENVIRONMENT_DISCOVERY
    else:
        st.current_state = JobStatus.ESCALATE
        st.escalated = True
        st.human_escalation_reason = tr.reason
        if tr.decision.value == "reject":
            emit(st, events.HUMAN_ESCALATION, tr.reason, agent="triage")
    _save(ctx, st)
    return _patch(st)


def node_environment_discovery(state: dict, ctx: WorkflowContext) -> dict:
    """Read-only inspection of the repo to choose an execution strategy.

    Runs before planning/implementation so incompatible repositories are
    identified cheaply (no clone, no agent tokens).
    """
    st = _load(ctx, state)
    st.current_state = JobStatus.ENVIRONMENT_DISCOVERY
    emit(st, events.ENVIRONMENT_DISCOVERY_STARTED, "Inspecting repository environment", agent="environment")
    try:
        if not ctx.settings.env_discovery_enabled:
            emit(st, events.ENVIRONMENT_DISCOVERY_COMPLETED, "disabled; assuming standard docker")
            st.current_state = JobStatus.PLAN
            _save(ctx, st)
            return _patch(st)
        from contributor.agents.environment import discover_environment_from_github

        owner, repo = st.repository.split("/", 1)
        ref = st.issue_metadata.get("default_branch", "HEAD")
        report = discover_environment_from_github(ctx.github, owner, repo, ref, issue_body=st.issue_body)
        st.environment_report = report
        emit(st, events.ENVIRONMENT_DISCOVERY_COMPLETED,
             f"languages={report.languages[:4]} tools={report.required_tools[:6]} "
             f"container_compatible={report.container_compatible} confidence={report.confidence}",
             agent="environment", data={"report": report.model_dump(mode="json")})
        emit(st, events.ENVIRONMENT_STRATEGY, f"strategy={report.strategy.value}", agent="environment",
             data={"strategy": report.strategy.value})
        if report.test_commands:
            st.issue_metadata["env_test_commands"] = report.test_commands
        st.current_state = JobStatus.PLAN
    except Exception as e:
        st.errors.append(f"environment discovery failed: {e}")
        emit(st, events.ENVIRONMENT_DISCOVERY_COMPLETED, f"error: {e}", agent="environment")
        st.current_state = JobStatus.PLAN
    _save(ctx, st)
    return _patch(st)


def node_plan(state: dict, ctx: WorkflowContext) -> dict:
    st = _load(ctx, state)
    st.current_state = JobStatus.PLAN
    ws = Path(st.workspace_path) if st.workspace_path else None
    plan = build_plan(st.issue_title, st.issue_body, st.triage_result, workspace=ws if ws and ws.exists() else None)
    st.plan = plan
    emit(st, events.PLAN_CREATED, plan.objective, agent="planner")
    st.current_state = JobStatus.PREPARE_WORKSPACE
    _save(ctx, st)
    return _patch(st)


def _env_cache_dir(st: JobState, ctx: WorkflowContext, plan) -> tuple[Path, bool]:
    """Return (env_cache_dir, cache_hit). Keyed by repo + bootstrap spec."""
    import hashlib

    root = Path(ctx.settings.env_cache_root).expanduser()
    key = hashlib.sha1(f"{st.repository}:{plan.cache_material()}".encode()).hexdigest()[:16]
    d = root / key
    cache_hit = d.exists() and any(d.iterdir())
    d.mkdir(parents=True, exist_ok=True)
    return d, cache_hit


def _provision_environment(st: JobState, ctx: WorkflowContext, ws: Path) -> bool:
    """Start the sandbox session, bootstrap the repo env, run preflight.

    Returns True when the environment is healthy and implementation may proceed.
    On failure sets a terminal state (ENVIRONMENT_FAILURE / RESOURCE_INCOMPATIBLE).
    """
    from contributor.models.state import EnvironmentReport
    from contributor.sandbox.bootstrap import detect_bootstrap, run_bootstrap
    from contributor.sandbox.health import run_preflight
    from contributor.sandbox.resources import choose_resource_profile

    report = st.environment_report or EnvironmentReport()
    emit(st, events.ENVIRONMENT_DISCOVERY_STARTED, "Provisioning isolated environment", agent="environment")

    selection = choose_resource_profile(report, requested=ctx.settings.resource_profile)
    st.resource_profile = selection.profile.name
    emit(st, events.ENVIRONMENT_DISCOVERY_COMPLETED,
         f"resource profile={selection.profile.name} cpus={selection.profile.cpus} mem={selection.profile.memory}",
         agent="environment", data={"warnings": selection.warnings, "reasons": selection.reasons})
    if not selection.compatible:
        st.current_state = JobStatus.RESOURCE_INCOMPATIBLE
        st.escalated = True
        st.human_escalation_reason = "Resource incompatible: " + "; ".join(selection.reasons)
        st.errors.append(st.human_escalation_reason)
        st.done = True
        emit(st, events.REPO_ENV_UNSUPPORTED, st.human_escalation_reason, agent="environment")
        return False

    plan = detect_bootstrap(ws, report)
    env_dir, cache_hit = _env_cache_dir(st, ctx, plan)
    st.issue_metadata["env_cache_hit"] = cache_hit
    try:
        session = ctx.sessions.get_or_create(
            st.job_id, workspace=ws, env_dir=env_dir, profile=selection.profile
        )
        session.start()
    except Exception as e:
        st.current_state = JobStatus.ENVIRONMENT_FAILURE
        st.escalated = True
        st.human_escalation_reason = f"Sandbox session failed to start: {e}"
        st.errors.append(st.human_escalation_reason)
        st.done = True
        emit(st, events.REPO_ENV_UNSUPPORTED, st.human_escalation_reason, agent="environment")
        return False

    bootstrap_report = None
    if ctx.settings.bootstrap_enabled and not plan.is_empty:
        emit(st, events.ENVIRONMENT_DISCOVERY_STARTED,
             f"bootstrap ecosystem={plan.ecosystem} commands={[c.label for c in plan.commands]}",
             agent="environment")
        bootstrap_report = run_bootstrap(session, plan, timeout_s=ctx.settings.bootstrap_timeout_s)
    st.bootstrap_report = bootstrap_report or {"ecosystem": plan.ecosystem, "commands": [], "ok": True}
    emit(st, events.ENVIRONMENT_DISCOVERY_COMPLETED,
         f"bootstrap ok={st.bootstrap_report.get('ok')} duration={st.bootstrap_report.get('duration_s')}s",
         agent="environment", data={"toolchain": plan.toolchain, "source": plan.source})

    health = run_preflight(
        session, plan,
        bootstrap_report=st.bootstrap_report,
        resource_warnings=selection.warnings,
    )
    st.environment_health = health.model_dump(mode="json")
    emit(st, events.ENVIRONMENT_DISCOVERY_COMPLETED,
         f"preflight healthy={health.healthy} missing={health.missing_tools} wrong={health.wrong_versions}",
         agent="environment", data=health.model_dump(mode="json"))
    if not health.healthy:
        st.current_state = JobStatus.ENVIRONMENT_FAILURE
        st.escalated = True
        reasons = health.missing_tools + health.wrong_versions + health.dependency_failures
        st.human_escalation_reason = "Environment preflight failed: " + "; ".join(reasons[:5])
        st.errors.append(st.human_escalation_reason)
        st.done = True
        emit(st, events.REPO_ENV_UNSUPPORTED, st.human_escalation_reason, agent="environment")
        return False
    return True


def node_prepare_workspace(state: dict, ctx: WorkflowContext) -> dict:
    st = _load(ctx, state)
    st.current_state = JobStatus.PREPARE_WORKSPACE
    try:
        ws = ctx.sandbox.create_workspace(st.job_id)
        st.workspace_path = str(ws)
        owner, repo = st.repository.split("/", 1)
        try:
            meta = ctx.github.get_repo(owner, repo)
            clone_url = meta.get("clone_url", f"https://github.com/{st.repository}.git")
            default_branch = meta.get("default_branch", "main")
        except Exception:
            clone_url = f"https://github.com/{st.repository}.git"
            default_branch = "main"
        st.issue_metadata["clone_url"] = clone_url
        st.issue_metadata["default_branch"] = default_branch
        # If workspace already has content (tests), skip clone
        if (ws / ".git").exists():
            pass
        else:
            # Clone into a temp sibling then move contents (git clone needs empty dir).
            # Clone into a temp sibling then move contents.
            import tempfile

            tmp = ws.parent / f"{ws.name}-clone-tmp"
            if tmp.exists():
                import shutil

                shutil.rmtree(tmp, ignore_errors=True)
            r = clone_repo(clone_url, tmp)
            if r.exit_code != 0:
                raise RuntimeError(f"clone failed: {r.stderr[-1000:]}")
            # move contents (including .git) into ws
            for child in tmp.iterdir():
                target = ws / child.name
                if target.exists():
                    if target.is_dir():
                        import shutil as _sh

                        _sh.rmtree(target, ignore_errors=True)
                    else:
                        target.unlink()
                child.rename(target)
            import shutil as _sh2

            _sh2.rmtree(tmp, ignore_errors=True)
        branch = make_branch_name(st.job_id, st.issue_number)
        st.branch_name = branch
        # Idempotent on resume: reuse the branch if it already exists.
        from contributor.execution.git import branch_exists, checkout_branch

        if branch_exists(ws, branch):
            cr = checkout_branch(ws, branch)
        else:
            cr = checkout_new_branch(ws, branch)
        if cr.exit_code != 0:
            raise RuntimeError(f"branch checkout failed: {cr.stderr[-500:]}")
        from contributor.execution.git import block_push, ensure_scratch_dir

        ensure_scratch_dir(ws)
        block_push(ws)
        emit(st, events.WORKSPACE_CREATED, f"workspace={ws} branch={branch}")
        # Provision the isolated environment (toolchain + deps + OpenCode) before
        # any agent work. Failures are terminal environment/resource states.
        provisioned = True
        if ctx.settings.sandbox_execution and ctx.sandbox.use_docker and ctx.sessions is not None:
            provisioned = _provision_environment(st, ctx, ws)
        if provisioned:
            st.current_state = JobStatus.IMPLEMENT
    except Exception as e:
        st.errors.append(f"workspace failed: {e}")
        emit(st, events.JOB_FAILED, str(e))
        st.current_state = JobStatus.ESCALATE
        st.escalated = True
        st.human_escalation_reason = f"Workspace preparation failed: {e}"
    _save(ctx, st)
    return _patch(st)


def _provider_of(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else ""


def _handle_opencode_failure(st: JobState, code: int, err: str, kind: str, *, agent: str) -> bool:
    """Tag provider-level failures so routing can stop instead of looping.

    Returns True when the failure is provider-level (retrying/debugging cannot help).
    """
    from contributor.opencode.runner import ERROR_PERMISSION_REJECTED, PROVIDER_BLOCKING_ERRORS

    if kind and kind in PROVIDER_BLOCKING_ERRORS:
        st.issue_metadata["provider_blocked"] = kind
        st.errors.append(f"{agent} provider failure [{kind}]: {err[-500:]}")
        return True
    if kind == ERROR_PERMISSION_REJECTED:
        # Agent tried to touch a path outside the repo. Retryable: guide it to
        # use the in-repo scratch dir on the next attempt.
        st.errors.append(
            f"{agent} permission rejected: stay inside the repository and use "
            f".contributor-scratch/ for scratch work (never /tmp or $HOME)."
        )
    elif code != 0:
        st.errors.append(f"{agent} opencode exit {code}: {err[-500:]}")
    return False


def _record_agent_usage(ctx: WorkflowContext, st: JobState, task: str, result, attempt: int) -> None:
    if not ctx.settings.usage_enabled:
        return
    from contributor.persistence.usage import record_usage

    record_usage(
        ctx.db,
        job_id=st.job_id,
        task=task,
        provider=_provider_of(result.model),
        model=result.model,
        variant=result.variant,
        tier=result.tier,
        duration_s=result.duration_s,
        attempt=attempt,
        outcome="ok" if result.code == 0 and not result.error_kind else (result.error_kind or f"exit_{result.code}"),
    )
    emit(st, events.USAGE_RECORDED,
         f"{task} tier={result.tier} model={result.model} variant={result.variant or '-'} "
         f"{result.duration_s:.1f}s",
         agent=task, model=result.model, attempt=attempt,
         data={"task": task, "tier": result.tier, "duration_s": result.duration_s})


def node_implement(state: dict, ctx: WorkflowContext) -> dict:
    st = _load(ctx, state)
    st.current_state = JobStatus.IMPLEMENT
    st.implementation_attempt += 1
    st.issue_metadata.pop("provider_blocked", None)
    emit(st, events.IMPLEMENTATION_STARTED, f"attempt {st.implementation_attempt}", agent="implementer", attempt=st.implementation_attempt)
    try:
        result = run_implementation(st, ctx.settings, _runner_for(st, ctx), health_store=ctx.health)
        emit(st, events.IMPLEMENTATION_COMPLETED, f"exit={result.code} kind={result.error_kind or '-'}",
             agent="implementer", attempt=st.implementation_attempt,
             data={"exit_code": result.code, "error_kind": result.error_kind,
                   "stdout_tail": result.stdout[-2000:], "stderr_tail": result.stderr[-2000:]})
        _record_agent_usage(ctx, st, "implementation", result, st.implementation_attempt)
        blocked = _handle_opencode_failure(st, result.code, result.stderr, result.error_kind, agent="implement")
        if blocked:
            # Provider errors must not consume implementation attempts.
            st.implementation_attempt = max(0, st.implementation_attempt - 1)
            try:
                from contributor.opencode.client import ModelSpec

                ctx.health.put(ModelSpec(provider=_provider_of(result.model), model=result.model,
                                         variant=result.variant), ok=False, error_class=result.error_kind)
            except Exception:
                pass
            emit(st, events.MODEL_UNAVAILABLE, f"{result.model} marked unavailable ({result.error_kind})",
                 model=result.model)
    except Exception as e:
        st.errors.append(f"implement failed: {e}")
        emit(st, events.IMPLEMENTATION_COMPLETED, f"error: {e}", agent="implementer")
    st.current_state = JobStatus.TEST
    _save(ctx, st)
    return _patch(st)


def node_test(state: dict, ctx: WorkflowContext) -> dict:
    st = _load(ctx, state)
    st.current_state = JobStatus.TEST
    emit(st, events.TEST_STARTED, "Running tests", attempt=st.implementation_attempt)
    try:
        ws = Path(st.workspace_path)
        suggested = list(st.plan.commands_to_run) if st.plan else []
        # Environment discovery may have found the repo's real test entrypoint.
        for cmd in st.issue_metadata.get("env_test_commands", []) or []:
            if cmd not in suggested:
                suggested.insert(0, cmd)
        session = ctx.sessions.get(st.job_id) if ctx.sessions is not None else None
        runner = TestRunner(ctx.sandbox, test_timeout_s=ctx.settings.test_timeout_s, session=session)
        tr = None
        # Prefer the smallest meaningful test set (changed/related tests) in every
        # mode; fall back to the repo's canonical suite when no targeted command
        # can be derived. This keeps live contributions from blindly running the
        # entire repository suite (resource + time safety).
        from contributor.execution.git import working_tree_files

        changed = working_tree_files(ws)
        likely = st.plan.files_expected_to_change if st.plan else []
        tr = runner.run_targeted(ws, changed_files=changed, likely_files=likely)
        if tr is not None:
            st.issue_metadata["targeted_test_command"] = tr.command
        if tr is None:
            tr = runner.run(ws, suggested=suggested)
        st.test_results.append(tr)
        if tr.passed:
            emit(st, events.TEST_PASSED, f"{tr.command} ({tr.duration_s:.1f}s)")
        elif tr.environment_related:
            emit(st, events.REPO_ENV_UNSUPPORTED,
                 f"{tr.command} failed on missing host tooling; not a code failure. failures={tr.failures[:3]}")
        else:
            emit(st, events.TEST_FAILED, f"{tr.command} exit={tr.exit_code} failures={tr.failures[:3]}")
    except Exception as e:
        st.errors.append(f"test exec failed: {e}")
        st.test_results.append(TestResult(command="(error)", exit_code=1, stdout="", stderr=str(e)[:2000], passed=False, failures=[str(e)[:300]]))
        emit(st, events.TEST_FAILED, str(e))
    _save(ctx, st)
    return _patch(st)


def node_blocked_provider(state: dict, ctx: WorkflowContext) -> dict:
    """Terminal: OpenCode could not perform inference (CASE A). Retrying the
    workflow cannot help; distinct from implementation failure."""
    st = _load(ctx, state)
    st.current_state = JobStatus.BLOCKED_PROVIDER
    st.escalated = True
    kind = st.issue_metadata.get("provider_blocked", "UNKNOWN_PROVIDER_ERROR")
    st.human_escalation_reason = (
        f"OpenCode provider failure [{kind}] prevented implementation. "
        "Fix provider auth/model/network or select a working model before retrying."
    )
    emit(st, events.HUMAN_ESCALATION, st.human_escalation_reason)
    st.done = True
    _save(ctx, st)
    return _patch(st)


def node_env_incompatible(state: dict, ctx: WorkflowContext) -> dict:
    """Terminal: repo tests cannot run in this sandbox (CASE B)."""
    st = _load(ctx, state)
    st.current_state = JobStatus.REPOSITORY_ENVIRONMENT_INCOMPATIBLE
    st.escalated = True
    eco = ""
    detail = ""
    if st.test_results:
        last = st.test_results[-1]
        eco = last.ecosystem
        if last.command == "(no test command)":
            detail = "no runnable test command discovered"
        else:
            detail = f"{len(last.failures)} failures dominated by missing host tooling"
    st.human_escalation_reason = (
        f"Repository tests cannot be verified in this sandbox for ecosystem {eco or 'unknown'} "
        f"({detail}). Implementation may be correct; needs a compatible test environment or an "
        "extended test-runner (missing host tools such as lua/magick/systemd)."
    )
    emit(st, events.REPO_ENV_UNSUPPORTED, st.human_escalation_reason)
    st.done = True
    _save(ctx, st)
    return _patch(st)


def node_environment_failure(state: dict, ctx: WorkflowContext) -> dict:
    """Terminal: tests failed because dependencies/environment are missing
    (CASE E) — distinct from a code bug and from a host-incompatible repo."""
    st = _load(ctx, state)
    st.current_state = JobStatus.ENVIRONMENT_FAILURE
    st.escalated = True
    detail = ""
    if st.test_results:
        last = st.test_results[-1]
        detail = f"{len(last.failures)} environment-related failures from `{last.command}`"
    st.human_escalation_reason = (
        f"Tests failed on missing dependencies/environment ({detail}). This is not a code "
        "failure; provide a custom sandbox image or dependency setup."
    )
    emit(st, events.REPO_ENV_UNSUPPORTED, st.human_escalation_reason)
    st.done = True
    _save(ctx, st)
    return _patch(st)


def node_resource_incompatible(state: dict, ctx: WorkflowContext) -> dict:
    """Terminal: the repository build cannot fit available host resources."""
    st = _load(ctx, state)
    st.current_state = JobStatus.RESOURCE_INCOMPATIBLE
    st.escalated = True
    if not st.human_escalation_reason:
        st.human_escalation_reason = "Repository build requires more resources than available."
    emit(st, events.REPO_ENV_UNSUPPORTED, st.human_escalation_reason)
    st.done = True
    _save(ctx, st)
    return _patch(st)


def node_review(state: dict, ctx: WorkflowContext) -> dict:
    st = _load(ctx, state)
    st.current_state = JobStatus.REVIEW
    st.review_attempt += 1
    emit(st, events.REVIEW_STARTED, f"review cycle {st.review_attempt}", agent="reviewer")
    try:
        ws = Path(st.workspace_path)
        diff = diff_full(ws)
        files = changed_files(ws)
        rr = review_change(diff=diff, changed_files=files, test_results=st.test_results, issue_title=st.issue_title,
                           plan_objective=st.plan.objective if st.plan else "")
        st.review_result = rr
        if rr.verdict.value == "approved":
            emit(st, events.REVIEW_APPROVED, rr.summary, agent="reviewer")
        else:
            emit(st, events.REVIEW_REJECTED, rr.summary, agent="reviewer")
    except Exception as e:
        st.errors.append(f"review failed: {e}")
        from contributor.models.state import ReviewResult as _RR, ReviewVerdict as _RV

        st.review_result = _RR(verdict=_RV.HUMAN_REQUIRED, summary=f"Reviewer error: {e}", confidence=0.3)
        emit(st, events.REVIEW_REJECTED, str(e), agent="reviewer")
    _save(ctx, st)
    return _patch(st)


def node_debug(state: dict, ctx: WorkflowContext) -> dict:
    st = _load(ctx, state)
    st.current_state = JobStatus.DEBUG
    st.debug_attempt += 1
    st.issue_metadata.pop("provider_blocked", None)
    emit(st, events.REPAIR_STARTED, f"debug attempt {st.debug_attempt}", agent="debugger", attempt=st.debug_attempt)
    try:
        result = run_debug(st, ctx.settings, _runner_for(st, ctx), health_store=ctx.health)
        emit(st, events.IMPLEMENTATION_COMPLETED, f"debug exit={result.code} kind={result.error_kind or '-'}",
             agent="debugger", attempt=st.debug_attempt,
             data={"exit_code": result.code, "error_kind": result.error_kind})
        _record_agent_usage(ctx, st, "debug", result, st.debug_attempt)
        blocked = _handle_opencode_failure(st, result.code, result.stderr, result.error_kind, agent="debug")
        if blocked:
            # Provider errors must not consume debug attempts.
            st.debug_attempt = max(0, st.debug_attempt - 1)
            try:
                from contributor.opencode.client import ModelSpec

                ctx.health.put(ModelSpec(provider=_provider_of(result.model), model=result.model,
                                         variant=result.variant), ok=False, error_class=result.error_kind)
            except Exception:
                pass
            emit(st, events.MODEL_UNAVAILABLE, f"{result.model} marked unavailable ({result.error_kind})",
                 model=result.model)
    except Exception as e:
        st.errors.append(f"debug failed: {e}")
    st.current_state = JobStatus.TEST
    _save(ctx, st)
    return _patch(st)


def _push_url(token: str, target: str) -> str:
    return f"https://x-access-token:{token}@github.com/{target}.git"


def _verify_remote_ref(st: JobState, ctx: WorkflowContext, url: str) -> bool:
    """Confirm the pushed branch exists on the remote at the expected commit.

    A PR is never created unless this succeeds.
    """
    if not ctx.settings.ci_verify_remote:
        st.remote_ref_verified = True
        st.remote_ref_sha = st.commit_sha
        return True
    from contributor.execution.git import remote_ref_sha

    sha = remote_ref_sha(url, st.branch_name)
    st.remote_ref_sha = sha
    if not sha or (st.commit_sha and sha != st.commit_sha):
        st.remote_ref_verified = False
        reason = (
            f"remote ref refs/heads/{st.branch_name} not found"
            if not sha
            else f"remote {sha[:12]} != pushed {st.commit_sha[:12]}"
        )
        emit(st, events.REMOTE_REF_UNVERIFIED, reason,
             data={"expected": st.commit_sha, "remote": sha})
        return False
    st.remote_ref_verified = True
    emit(st, events.REMOTE_REF_VERIFIED, f"{st.branch_name} @ {sha[:12]}",
         data={"sha": sha})
    return True


def _open_or_update_pr(st: JobState, ctx: WorkflowContext, ws: Path) -> bool:
    """Create/reuse the PR after a verified push. Returns True on success."""
    from contributor.execution.git import working_tree_files

    changed = [f for f in working_tree_files(ws) if not f.startswith(".contributor")]
    if not changed and st.commit_sha:
        from contributor.execution.commands import run_command

        r = run_command(
            ["git", "-C", str(ws), "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            timeout=30,
        )
        if r.ok:
            changed = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    base = ""
    if isinstance(st.issue_metadata, dict):
        base = str(st.issue_metadata.get("default_branch", "") or "")
    base = base or "main"
    emit(st, events.PR_CREATE_STARTED,
         f"PR -> {st.repository} base={base} head={ctx.push_repo or st.repository}")
    try:
        data, action, target = publish_pr(
            ctx.github, st, ctx.settings,
            push_target=ctx.push_repo or st.repository,
            base_branch=base, changed_files=changed,
        )
    except Exception as e:
        st.errors.append(f"PR creation failed after push: {e}")
        st.current_state = JobStatus.PR_CREATE_FAILED
        st.escalated = True
        st.human_escalation_reason = f"PR creation failed after push: {e}"
        emit(st, events.PR_CREATE_FAILED, st.human_escalation_reason)
        st.done = True
        return False
    if not st.pull_request_url or not st.pull_request_number:
        # The API did not actually return a PR: never claim PR_OPEN.
        st.current_state = JobStatus.PR_CREATE_FAILED
        st.escalated = True
        st.human_escalation_reason = "PR API returned no url/number"
        emit(st, events.PR_CREATE_FAILED, st.human_escalation_reason, data={"response": data})
        st.done = True
        return False
    st.current_state = JobStatus.PR_OPEN
    st.merge_ready = False
    ev = events.PR_REUSED if (st.pr_reused or action == "reused") else events.PR_OPENED
    emit(st, ev, f"{action} {st.pull_request_url}",
         data={"number": st.pull_request_number, "head": target.head_ref,
               "base": target.base_branch, "reused": st.pr_reused})
    return True


def node_create_pr(state: dict, ctx: WorkflowContext) -> dict:
    """Publish a contribution: gate -> commit -> push -> verify ref -> PR.

    Benchmark mode is hard-blocked. The branch is pushed before any PR is
    attempted, and the PR is never created unless the remote ref is verified.
    """
    from contributor.execution.git import has_changes as _hc, head_sha, log_last
    from contributor.execution.push_gate import run_push_gate
    from contributor.execution.git import push_to_target

    st = _load(ctx, state)
    st.current_state = JobStatus.CREATE_PR
    st.execution_mode = ctx.execution_mode
    st.allow_push = ctx.allow_push
    st.allow_create_pr = ctx.allow_create_pr
    st.ci_monitor = ctx.ci_monitor
    st.auto_merge = ctx.auto_merge

    if ctx.benchmark_mode:
        st.errors.append("benchmark mode forbids any GitHub mutation")
        st.current_state = JobStatus.PUSH_GATE_REJECTED
        st.escalated = True
        st.human_escalation_reason = "benchmark mode forbids push"
        emit(st, events.PUSH_GATE_REJECTED, st.human_escalation_reason)
        st.done = True
        _save(ctx, st)
        return _patch(st)

    ws = Path(st.workspace_path) if st.workspace_path else Path(".")
    target = ctx.push_repo or st.repository
    st.push_target = target
    st.push_remote = ctx.push_remote
    st.auto_merge = bool(ctx.auto_merge)

    # 1. hard deterministic gate on the uncommitted contribution.
    gate = run_push_gate(
        job=st, workspace=ws, settings=ctx.settings, allow_push=ctx.allow_push,
        execution_mode=ctx.execution_mode, target_repo=target,
        remote=ctx.push_remote, expected_repo=st.repository,
    )
    st.push_gate = gate.to_dict()
    if not gate.allowed:
        st.current_state = JobStatus.PUSH_GATE_REJECTED
        st.escalated = True
        st.human_escalation_reason = "push gate rejected: " + "; ".join(gate.reasons[:6])
        st.errors.append(st.human_escalation_reason)
        emit(st, events.PUSH_GATE_REJECTED, st.human_escalation_reason, data=st.push_gate)
        st.done = True
        _save(ctx, st)
        return _patch(st)
    emit(st, events.PUSH_GATE_PASSED, "deterministic push gate passed", data=st.push_gate)

    # 2. exactly one coherent contribution commit (orchestrator-owned).
    try:
        if _hc(ws):
            msg = f"Fix #{st.issue_number}: {st.issue_title[:72]}"
            cr = commit_all(ws, msg)
            if cr.exit_code != 0:
                raise RuntimeError(f"commit failed: {cr.stderr[-800:]}")
            st.commit_sha = head_sha(ws)
            emit(st, events.COMMIT_CREATED, f"{st.commit_sha[:12]} {msg}",
                 data={"sha": st.commit_sha, "message": msg, "log": log_last(ws)})
        elif st.commit_sha:
            pass  # already committed
        else:
            raise RuntimeError("No changes to publish (empty diff).")
    except Exception as e:
        st.errors.append(f"commit failed: {e}")
        emit(st, events.JOB_FAILED, str(e))
        st.current_state = JobStatus.ESCALATE
        st.escalated = True
        st.human_escalation_reason = f"Commit failed: {e}"
        _save(ctx, st)
        return _patch(st)

    # 3. orchestrator-owned push using the controlled credential.
    token = ctx.settings.github_token
    if not token:
        st.current_state = JobStatus.PUSH_FAILED
        st.escalated = True
        st.human_escalation_reason = "push failed: no GITHUB_TOKEN configured"
        st.push_result = {"target": target, "ok": False, "reason": st.human_escalation_reason}
        emit(st, events.PUSH_FAILED, st.human_escalation_reason, data=st.push_result)
        st.done = True
        _save(ctx, st)
        return _patch(st)

    url = _push_url(token, target)
    emit(st, events.PUSH_STARTED, f"pushing {st.branch_name} -> {target}")
    pushed = push_to_target(ws, st.branch_name, url=url)
    st.push_result = {
        "target": target,
        "remote": ctx.push_remote,
        "branch": st.branch_name,
        "commit_sha": st.commit_sha,
        "exit_code": pushed.exit_code,
        "ok": pushed.exit_code == 0,
        "stdout_tail": pushed.stdout[-2000:],
        "stderr_tail": pushed.stderr[-2000:],
    }
    if pushed.exit_code != 0:
        st.current_state = JobStatus.PUSH_FAILED
        st.escalated = True
        st.human_escalation_reason = f"push failed: {pushed.stderr[-500:]}"
        st.errors.append(st.human_escalation_reason)
        emit(st, events.PUSH_FAILED, st.human_escalation_reason, data=st.push_result)
        st.done = True
        _save(ctx, st)
        return _patch(st)

    emit(st, events.PUSHED, f"{target} {st.branch_name} {st.commit_sha[:12]}", data=st.push_result)

    # 4. verify the remote ref BEFORE any PR can be created.
    if not _verify_remote_ref(st, ctx, url):
        st.current_state = JobStatus.PUSH_UNVERIFIED
        st.escalated = True
        st.human_escalation_reason = "pushed branch could not be verified on the remote"
        st.errors.append(st.human_escalation_reason)
        st.done = True
        _save(ctx, st)
        return _patch(st)

    # 5. PR (live PR mode only).
    if ctx.allow_create_pr:
        ok = _open_or_update_pr(st, ctx, ws)
        if not ok:
            st.done = True
        elif not ctx.ci_monitor:
            st.done = True  # PR opened; CI monitoring disabled in this mode
    else:
        st.current_state = JobStatus.PUSHED
        st.human_escalation_reason = ""
        st.done = True
    _save(ctx, st)
    return _patch(st)


def _ensure_session(st: JobState, ctx: WorkflowContext, ws: Path):
    """Return a live sandbox session, re-provisioning deterministically if the
    process restarted since the session was created."""
    if ctx.sessions is None:
        return None
    session = ctx.sessions.get(st.job_id)
    if session is not None:
        return session
    if not (ctx.settings.sandbox_execution and ctx.sandbox.use_docker):
        return None
    if not _provision_environment(st, ctx, ws):
        return None
    return ctx.sessions.get(st.job_id)


def _reproduce_ci(st: JobState, ctx: WorkflowContext, ws: Path, session) -> dict | None:
    """Try to reproduce the CI failure locally with the narrowest test."""
    try:
        from contributor.execution.git import working_tree_files
        from contributor.execution.tests import TestRunner

        changed = working_tree_files(ws)
        likely = st.plan.files_expected_to_change if st.plan else []
        runner = TestRunner(ctx.sandbox, test_timeout_s=ctx.settings.test_timeout_s, session=session)
        tr = runner.run_targeted(ws, changed_files=changed, likely_files=likely)
        if tr is None:
            return {"reproduced": False, "reason": "no targeted command derivable"}
        st.test_results.append(tr)
        result = {
            "command": tr.command,
            "passed": tr.passed,
            "exit_code": tr.exit_code,
            "reproduced": not tr.passed,
            "failures": tr.failures[:5],
        }
        emit(st, events.CI_REPRODUCED,
             f"local repro `{tr.command}` exit={tr.exit_code} reproduced={not tr.passed}",
             data=result)
        return result
    except Exception as e:
        return {"reproduced": False, "reason": str(e)[:200]}


def _attempt_ci_retry(st: JobState, ctx: WorkflowContext, owner: str, repo: str, ci) -> bool:
    """Bounded, code-free retry for flaky/infrastructure CI. Never repairs."""
    from contributor.github.ci import actions_run_id_from_url

    if st.ci_retry_count >= ctx.settings.ci_flaky_retries:
        return False
    run_ids = {
        actions_run_id_from_url(c.get("details_url") or c.get("url") or "")
        for c in ci.checks
        if c.get("state") == "fail"
    }
    run_ids.discard("")
    if not run_ids or not hasattr(ctx.github, "rerun_actions_run"):
        return False
    requested = False
    for rid in sorted(run_ids):
        if ctx.github.rerun_actions_run(owner, repo, rid):
            requested = True
    if requested:
        st.ci_retry_count += 1
        emit(st, events.CI_RETRY_REQUESTED,
             f"requested CI rerun (attempt {st.ci_retry_count})", data={"run_ids": sorted(run_ids)})
    return requested


def node_ci_check(state: dict, ctx: WorkflowContext) -> dict:
    """Bounded CI monitoring: fetch normalized checks, classify failures.

    The verdict comes from GitHub check states, never from the model.
    """
    from contributor.github.ci import (
        apply_required,
        classify_with_diagnosis,
        interpret_ci,
        watch_ci,
    )
    from contributor.execution.git import head_sha

    st = _load(ctx, state)
    st.current_state = JobStatus.CI_PENDING
    ws = Path(st.workspace_path) if st.workspace_path else Path(".")
    sha = st.commit_sha or head_sha(ws)
    if not st.repository or "/" not in st.repository or not sha:
        st.current_state = JobStatus.CI_UNKNOWN
        st.ci_result = CIResult(state="unknown", summary="missing repo or sha")
        emit(st, events.CI_UNKNOWN, "cannot query CI (missing repo/sha)")
        _save(ctx, st)
        return _patch(st)
    owner, repo = st.repository.split("/", 1)
    required: list[str] = []
    if hasattr(ctx.github, "get_branch_required_checks"):
        try:
            required = ctx.github.get_branch_required_checks(
                owner, repo, str(st.issue_metadata.get("default_branch", "") or "")
            )
        except Exception:
            required = []

    emit(st, events.CI_PENDING, f"monitoring checks for {sha[:12]}")
    # Persist an explicit pending/running state before the (bounded) wait so an
    # interrupt can resume from the real phase.
    try:
        pre = apply_required(interpret_ci(ctx.github.get_ci_status(owner, repo, sha)), required)
        st.ci_checks, st.ci_result, st.ci_polls = pre.checks, pre, 1
        if pre.phase == "in_progress":
            st.current_state = JobStatus.CI_RUNNING
            emit(st, events.CI_RUNNING, pre.summary)
        _save(ctx, st)
    except Exception:
        pass

    # Bounded poll; re-run once per bounded flaky/infra retry.
    while True:
        try:
            ci = watch_ci(
                ctx.github, owner, repo, sha,
                interval_s=ctx.settings.ci_poll_interval_s,
                max_wait_s=ctx.settings.ci_max_wait_s,
                settle_s=ctx.settings.ci_settle_s,
            )
        except Exception as e:
            st.errors.append(f"ci check failed: {e}")
            st.current_state = JobStatus.CI_UNKNOWN
            st.ci_result = CIResult(state="unknown", summary=str(e)[:200])
            st.done = True
            _save(ctx, st)
            return _patch(st)
        ci = apply_required(ci, required)
        st.ci_checks, st.ci_result = ci.checks, ci
        st.ci_polls += ci.polls
        st.ci_wait_s = round(st.ci_wait_s + ci.waited_s, 1)
        if ci.timed_out:
            st.current_state = JobStatus.CI_FAILED
            from contributor.models.state import CIFailureClass

            st.ci_failure_class = CIFailureClass.CI_INFRASTRUCTURE_FAILURE
            st.human_escalation_reason = (
                f"CI did not complete within {ctx.settings.ci_max_wait_s}s "
                f"(polls={st.ci_polls}, checks={len(ci.checks)})."
            )
            emit(st, events.CI_TIMED_OUT, st.human_escalation_reason)
            break
        if ci.state == "pass":
            st.current_state = JobStatus.CI_PASSED
            st.human_escalation_reason = ""
            emit(st, events.CI_PASSED, ci.summary)
            break
        if ci.state == "unknown":
            st.current_state = JobStatus.CI_UNKNOWN
            st.human_escalation_reason = (
                "No GitHub CI checks were reported for this commit."
            )
            emit(st, events.CI_UNKNOWN, st.human_escalation_reason)
            break
        # failure: classify deterministically, then decide.
        cls, diag, raw = classify_with_diagnosis(
            ctx.github, owner, repo, ci, st.environment_report
        )
        st.ci_failure_class = cls
        st.ci_diagnosis = diag
        st.ci_raw_ref = raw
        st.current_state = JobStatus.CI_FAILED
        emit(st, events.CI_FAILED, f"class={cls.value} {ci.summary}", data={"class": cls.value})
        emit(st, events.CI_FAILURE_CLASSIFIED,
             f"{cls.value} repairable={cls.repairable}",
             data={"class": cls.value, "repairable": cls.repairable})
        if cls.repairable:
            break
        if _attempt_ci_retry(st, ctx, owner, repo, ci):
            _save(ctx, st)
            continue
        break
    _save(ctx, st)
    return _patch(st)


def node_ci_repair(state: dict, ctx: WorkflowContext) -> dict:
    """Bounded CI repair: reproduce locally, ask OpenCode for the smallest fix.

    The repair then flows through the existing test -> review -> publish loop,
    which re-applies the deterministic push gate and updates the same PR.
    """
    from contributor.agents.ci_repair import run_ci_repair

    st = _load(ctx, state)
    st.ci_repair_attempt += 1
    st.current_state = JobStatus.CI_REPAIRING
    kind = st.ci_failure_class.value if st.ci_failure_class else "unknown"
    emit(st, events.REPAIR_STARTED,
         f"CI repair attempt {st.ci_repair_attempt} ({kind})",
         agent="ci_repair", attempt=st.ci_repair_attempt,
         data={"class": kind})
    ws = Path(st.workspace_path) if st.workspace_path else None
    if ws is None or not ws.exists():
        st.errors.append("CI repair: workspace missing")
        st.current_state = JobStatus.ENVIRONMENT_FAILURE
        st.escalated = True
        st.human_escalation_reason = "CI repair requires the contribution workspace, which is gone."
        st.done = True
        _save(ctx, st)
        return _patch(st)

    session = _ensure_session(st, ctx, ws)
    if (
        session is None
        and ctx.sessions is not None
        and ctx.settings.sandbox_execution
        and ctx.sandbox.use_docker
    ):
        st.errors.append("CI repair: sandbox session unavailable")
        st.current_state = JobStatus.ENVIRONMENT_FAILURE
        st.escalated = True
        st.human_escalation_reason = "CI repair could not provision a sandbox session."
        st.done = True
        _save(ctx, st)
        return _patch(st)

    # 1. reproduce the failure locally (narrowest test), before the repair.
    _reproduce_ci(st, ctx, ws, session)

    # 2. run the bounded repair agent in the SAME sandbox/repository.
    try:
        result = run_ci_repair(
            st, ctx.settings, _runner_for(st, ctx), health_store=ctx.health,
        )
        emit(st, events.REPAIR_COMPLETE,
             f"repair exit={result.code} kind={result.error_kind or '-'}",
             agent="ci_repair", attempt=st.ci_repair_attempt,
             data={"exit_code": result.code, "error_kind": result.error_kind})
        _record_agent_usage(ctx, st, "ci_repair", result, st.ci_repair_attempt)
        blocked = _handle_opencode_failure(
            st, result.code, result.stderr, result.error_kind, agent="ci_repair"
        )
        if blocked:
            st.ci_repair_attempt = max(0, st.ci_repair_attempt - 1)
    except Exception as e:
        st.errors.append(f"ci repair failed: {e}")
        emit(st, events.JOB_FAILED, f"ci repair error: {e}", agent="ci_repair")
    st.current_state = JobStatus.TEST
    _save(ctx, st)
    return _patch(st)


def node_ci_repair_exhausted(state: dict, ctx: WorkflowContext) -> dict:
    st = _load(ctx, state)
    st.current_state = JobStatus.CI_REPAIR_EXHAUSTED
    st.escalated = True
    kind = st.ci_failure_class.value if st.ci_failure_class else "unknown"
    st.human_escalation_reason = (
        f"CI still failing after {st.ci_repair_attempt} repair attempt(s) "
        f"(last class={kind}). Check count={len(st.ci_checks)}."
    )
    emit(st, events.CI_REPAIR_EXHAUSTED, st.human_escalation_reason)
    st.done = True
    _save(ctx, st)
    return _patch(st)


def node_merge_ready(state: dict, ctx: WorkflowContext) -> dict:
    """Terminal: CI passed (or the repo exposes no checks). NEVER merges."""
    st = _load(ctx, state)
    st.current_state = JobStatus.MERGE_READY
    st.merge_ready = True
    st.escalated = False
    st.done = True
    if st.ci_result and st.ci_result.state == "pass":
        emit(st, events.MERGE_READY,
             f"PR {st.pull_request_url} ready for human merge (CI passed)",
             data={"ci_state": st.ci_result.state, "checks": len(st.ci_result.checks)})
    else:
        emit(st, events.MERGE_READY,
             f"PR {st.pull_request_url} ready for human merge (no CI checks reported)",
             data={"ci_state": st.ci_result.state if st.ci_result else "unknown"})
    _save(ctx, st)
    return _patch(st)



def node_update_pr(state: dict, ctx: WorkflowContext) -> dict:
    """Republish after a CI repair: gate -> one repair commit -> push -> update PR.

    Reuses the same branch and the same PR (never creates a duplicate), and
    re-applies the deterministic gate before the push.
    """
    from contributor.execution.git import has_changes as _hc, head_sha
    from contributor.execution.push_gate import run_push_gate
    from contributor.execution.git import push_to_target

    st = _load(ctx, state)
    st.current_state = JobStatus.UPDATE_PR
    if ctx.benchmark_mode:
        st.errors.append("benchmark mode forbids PR updates")
        st.current_state = JobStatus.PUSH_GATE_REJECTED
        st.escalated = True
        st.human_escalation_reason = "benchmark mode forbids PR updates"
        st.done = True
        _save(ctx, st)
        return _patch(st)

    ws = Path(st.workspace_path) if st.workspace_path else Path(".")
    target = ctx.push_repo or st.repository
    st.push_target = target

    gate = run_push_gate(
        job=st, workspace=ws, settings=ctx.settings, allow_push=ctx.allow_push,
        execution_mode=ctx.execution_mode, target_repo=target,
        remote=ctx.push_remote, expected_repo=st.repository,
    )
    st.push_gate = gate.to_dict()
    if not gate.allowed:
        st.current_state = JobStatus.PUSH_GATE_REJECTED
        st.escalated = True
        st.human_escalation_reason = "repair push gate rejected: " + "; ".join(gate.reasons[:6])
        st.errors.append(st.human_escalation_reason)
        emit(st, events.PUSH_GATE_REJECTED, st.human_escalation_reason, data=st.push_gate)
        st.done = True
        _save(ctx, st)
        return _patch(st)
    emit(st, events.PUSH_GATE_PASSED, "deterministic push gate passed for repair", data=st.push_gate)

    parent_sha = st.commit_sha
    if _hc(ws):
        kind = st.ci_failure_class.value if st.ci_failure_class else "ci"
        msg = f"CI repair for #{st.issue_number} ({kind})"
        cr = commit_all(ws, msg)
        if cr.exit_code != 0:
            st.errors.append(f"repair commit failed: {cr.stderr[-500:]}")
            st.current_state = JobStatus.ESCALATE
            st.escalated = True
            st.human_escalation_reason = f"Repair commit failed: {cr.stderr[-300:]}"
            _save(ctx, st)
            return _patch(st)
        st.commit_sha = head_sha(ws)
        emit(st, events.REPAIR_COMMIT_CREATED, f"{st.commit_sha[:12]} {msg}",
             data={"sha": st.commit_sha, "parent": parent_sha, "message": msg})
    else:
        st.errors.append("repair produced no changes; nothing to push")
        st.current_state = JobStatus.CI_REPAIR_EXHAUSTED
        st.escalated = True
        st.human_escalation_reason = "CI repair produced no change; cannot resolve the failure automatically."
        st.done = True
        _save(ctx, st)
        return _patch(st)

    token = ctx.settings.github_token
    if not token:
        st.current_state = JobStatus.PUSH_FAILED
        st.escalated = True
        st.human_escalation_reason = "repair push failed: no GITHUB_TOKEN configured"
        st.done = True
        _save(ctx, st)
        return _patch(st)
    url = _push_url(token, target)
    pushed = push_to_target(ws, st.branch_name, url=url)
    st.push_result = {
        "target": target, "remote": ctx.push_remote, "branch": st.branch_name,
        "commit_sha": st.commit_sha, "exit_code": pushed.exit_code,
        "ok": pushed.exit_code == 0,
        "stdout_tail": pushed.stdout[-2000:], "stderr_tail": pushed.stderr[-2000:],
        "repair": True,
    }
    if pushed.exit_code != 0:
        st.current_state = JobStatus.PUSH_FAILED
        st.escalated = True
        st.human_escalation_reason = f"repair push failed: {pushed.stderr[-500:]}"
        st.errors.append(st.human_escalation_reason)
        emit(st, events.PUSH_FAILED, st.human_escalation_reason, data=st.push_result)
        st.done = True
        _save(ctx, st)
        return _patch(st)
    emit(st, events.REPAIR_PUSHED, f"{target} {st.branch_name} {st.commit_sha[:12]}",
         data=st.push_result)

    if not _verify_remote_ref(st, ctx, url):
        st.current_state = JobStatus.PUSH_UNVERIFIED
        st.escalated = True
        st.human_escalation_reason = "repair push could not be verified on the remote"
        st.done = True
        _save(ctx, st)
        return _patch(st)

    review = st.review_result
    st.repair_history.append({
        "parent_sha": parent_sha,
        "new_sha": st.commit_sha,
        "reason": (st.ci_failure_class.value if st.ci_failure_class else "unknown"),
        "tests": st.test_results[-1].command if st.test_results else "",
        "tests_passed": bool(st.test_results and st.test_results[-1].passed),
        "review": review.verdict.value if review else "",
    })

    # Update the existing PR (never create a duplicate).
    if ctx.allow_create_pr and st.pull_request_number:
        try:
            publish_pr(
                ctx.github, st, ctx.settings,
                push_target=target,
                base_branch=str(st.issue_metadata.get("default_branch", "") or "main"),
            )
            emit(st, events.PR_UPDATED, st.pull_request_url)
        except Exception as e:
            st.errors.append(f"PR update after repair failed: {e}")
    st.current_state = JobStatus.CI_REPAIRED
    _save(ctx, st)
    return _patch(st)


def node_wait_review(state: dict, ctx: WorkflowContext) -> dict:
    st = _load(ctx, state)
    st.current_state = JobStatus.WAIT_FOR_REVIEW
    # Check for new maintainer feedback since PR creation. Deterministic: if review
    # comments exist mentioning changes, route back to implement.
    try:
        owner, repo = st.repository.split("/", 1)
        comments = ctx.github.list_pr_comments(owner, repo, st.pull_request_number or 0)
        try:
            reviews = ctx.github.list_review_comments(owner, repo, st.pull_request_number or 0)
        except Exception:
            reviews = []
        blob = "\n".join(str(c.get("body", "")) for c in list(comments) + list(reviews)).lower()
        wants_changes = any(k in blob for k in ["changes requested", "please fix", "needs work", "request changes", "fix this"])
        st.issue_metadata["pr_feedback_detected"] = bool(wants_changes)
        if wants_changes:
            st.issue_metadata["pr_feedback"] = blob[-2000:]
            emit(st, events.REPAIR_STARTED, "New reviewer feedback detected; re-entering repair loop")
            st.current_state = JobStatus.IMPLEMENT
        else:
            emit(st, events.JOB_COMPLETED, f"PR {st.pull_request_url} awaiting maintainer review")
            st.done = True
            st.current_state = JobStatus.DONE
    except Exception as e:
        st.errors.append(f"wait_review failed: {e}")
        st.done = True
        st.current_state = JobStatus.DONE
    _save(ctx, st)
    return _patch(st)


def node_finalize_benchmark(state: dict, ctx: WorkflowContext) -> dict:
    """Terminal node for benchmark mode: review approved, but NO push/PR.

    The final local diff and workspace remain on disk for inspection.
    """
    st = _load(ctx, state)
    approved = bool(st.review_result and st.review_result.verdict.value == "approved")
    if approved:
        st.current_state = JobStatus.DONE
        st.done = True
        emit(st, events.JOB_COMPLETED, "Benchmark run complete (review approved; no PR created)")
    else:
        st.current_state = JobStatus.ESCALATE
        st.escalated = True
        st.human_escalation_reason = st.human_escalation_reason or "Benchmark: review not approved"
        st.done = True
        emit(st, events.HUMAN_ESCALATION, st.human_escalation_reason)
    _save(ctx, st)
    return _patch(st)


def node_escalate(state: dict, ctx: WorkflowContext) -> dict:
    st = _load(ctx, state)
    st.current_state = JobStatus.ESCALATE
    st.escalated = True
    if not st.human_escalation_reason:
        st.human_escalation_reason = "; ".join(st.errors[-3:]) or "Escalated by workflow routing (limits or rejection)."
    emit(st, events.HUMAN_ESCALATION, st.human_escalation_reason)
    st.done = True
    _save(ctx, st)
    return _patch(st)
