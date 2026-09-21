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
from contributor.github.pull_requests import create_pr_for_job, render_pr_body, update_pr_body
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
        cr = checkout_new_branch(ws, branch)
        if cr.exit_code != 0:
            raise RuntimeError(f"branch checkout failed: {cr.stderr[-500:]}")
        from contributor.execution.git import ensure_scratch_dir

        ensure_scratch_dir(ws)
        emit(st, events.WORKSPACE_CREATED, f"workspace={ws} branch={branch}")
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
        result = run_implementation(st, ctx.settings, ctx.runner, health_store=ctx.health)
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
        tr = TestRunner(ctx.sandbox, test_timeout_s=ctx.settings.test_timeout_s).run(ws, suggested=suggested)
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
        result = run_debug(st, ctx.settings, ctx.runner, health_store=ctx.health)
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


def node_create_pr(state: dict, ctx: WorkflowContext) -> dict:
    st = _load(ctx, state)
    st.current_state = JobStatus.CREATE_PR
    try:
        ws = Path(st.workspace_path)
        if not has_changes(ws) and not st.pull_request_url:
            # nothing to commit — check diff against HEAD; if empty, escalate
            from contributor.execution.git import diff_full as _d

            if not _d(ws).strip():
                raise RuntimeError("No changes to create PR from (empty diff).")
        msg = f"Fix #{st.issue_number}: {st.issue_title[:72]}"
        cr = commit_all(ws, msg)
        if cr.exit_code != 0:
            raise RuntimeError(f"commit failed: {cr.stderr[-800:]}")
        # push with token auth
        import os

        env = dict(os.environ)
        token = ctx.settings.github_token
        if token:
            # embed token via http.extraHeader instead of URL to avoid leaking in logs
            env["GIT_HTTP_EXTRAHEADER"] = ""
        pr = push_branch(ws, st.branch_name, env={"GITHUB_TOKEN": token} if token else None)
        # push_branch uses git push origin; for token auth, rewrite origin URL if needed
        if pr.exit_code != 0:
            # try token-in-url fallback for https origins
            if token:
                from contributor.execution.commands import run_command as _rc

                owner_repo = st.repository
                _rc(["git", "-C", str(ws), "remote", "set-url", "origin",
                     f"https://x-access-token:{token}@github.com/{owner_repo}.git"], timeout=30)
                pr = push_branch(ws, st.branch_name)
            if pr.exit_code != 0:
                raise RuntimeError(f"push failed: {pr.stderr[-1000:]}")
        base = st.issue_metadata.get("default_branch", "main")
        data = create_pr_for_job(ctx.github, st, ctx.settings, base=base)
        st.pull_request_url = data.get("html_url", "")
        st.pull_request_number = int(data.get("number", 0) or 0)
        emit(st, events.PR_CREATED, st.pull_request_url)
        st.current_state = JobStatus.PR_CI_CHECK
    except Exception as e:
        st.errors.append(f"create_pr failed: {e}")
        emit(st, events.JOB_FAILED, str(e))
        st.current_state = JobStatus.ESCALATE
        st.escalated = True
        st.human_escalation_reason = f"PR creation failed: {e}"
    _save(ctx, st)
    return _patch(st)


def node_ci_check(state: dict, ctx: WorkflowContext) -> dict:
    st = _load(ctx, state)
    st.current_state = JobStatus.PR_CI_CHECK
    try:
        owner, repo = st.repository.split("/", 1)
        # resolve head sha via PR info
        sha = ""
        try:
            from contributor.execution.commands import run_command as _rc

            ws = Path(st.workspace_path)
            r = _rc(["git", "-C", str(ws), "rev-parse", "HEAD"], timeout=15)
            sha = r.stdout.strip()
        except Exception:
            pass
        if sha:
            ci = fetch_ci(ctx.github, owner, repo, sha)
        else:
            ci = CIResult(state="unknown", summary="no sha")
        st.ci_result = ci
        if ci.state == "pass":
            emit(st, events.CI_PASSED, ci.summary)
            st.current_state = JobStatus.WAIT_FOR_REVIEW
        elif ci.state == "pending":
            emit(st, events.CI_PASSED, f"CI pending: {ci.summary}")
            st.current_state = JobStatus.WAIT_FOR_REVIEW
        elif ci.state == "unknown":
            emit(st, events.CI_PASSED, "CI unknown (no checks); proceeding to review gate")
            st.current_state = JobStatus.WAIT_FOR_REVIEW
        else:
            from contributor.github.ci import classify_ci_failure

            cls = classify_ci_failure(ci, st.environment_report)
            st.ci_failure_class = cls
            st.ci_repair_attempt += 1
            emit(st, events.CI_CLASSIFIED, f"CI failed: class={cls.value} {ci.summary}", data={"class": cls.value})
            if cls.value == "environment_failure":
                st.current_state = JobStatus.ENVIRONMENT_FAILURE
            elif cls.value == "ci_infrastructure_failure":
                st.current_state = JobStatus.ESCALATE
                st.escalated = True
                st.human_escalation_reason = f"CI infrastructure failure (not a code failure): {ci.summary}"
            else:
                st.current_state = JobStatus.DEBUG
    except Exception as e:
        st.errors.append(f"ci check failed: {e}")
        st.current_state = JobStatus.WAIT_FOR_REVIEW
    _save(ctx, st)
    return _patch(st)


def node_update_pr(state: dict, ctx: WorkflowContext) -> dict:
    st = _load(ctx, state)
    st.current_state = JobStatus.UPDATE_PR
    try:
        ws = Path(st.workspace_path)
        from contributor.execution.git import has_changes as _hc, commit_all as _ca, push_branch as _pb

        if _hc(ws):
            cr = _ca(ws, f"Address review feedback for #{st.issue_number}")
            if cr.exit_code != 0:
                raise RuntimeError(f"commit failed: {cr.stderr[-500:]}")
            pr = _pb(ws, st.branch_name)
            if pr.exit_code != 0:
                raise RuntimeError(f"push failed: {pr.stderr[-500:]}")
        owner, repo = st.repository.split("/", 1)
        ctx.github.update_pr(owner, repo, st.pull_request_number, body=render_pr_body(st, ctx.settings))
        emit(st, events.PR_UPDATED, st.pull_request_url)
        st.current_state = JobStatus.PR_CI_CHECK
    except Exception as e:
        st.errors.append(f"update_pr failed: {e}")
        emit(st, events.JOB_FAILED, str(e))
        st.current_state = JobStatus.ESCALATE
        st.escalated = True
        st.human_escalation_reason = f"PR update failed: {e}"
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
