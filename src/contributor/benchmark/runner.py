"""Benchmark orchestration.

Pipeline per benchmark:
    discover (larger pool) -> structured triage -> environment discovery
    -> transparent selection -> execute each issue through the existing graph
    (benchmark_mode: no push / no PR) -> targeted tests -> broader suite
    -> independent review -> classify + report.

Reuses the existing workflow unchanged apart from the benchmark terminal path.
"""
from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contributor.agents.environment import discover_environment_from_github
from contributor.agents.triage import assess_solvability, triage_issue
from contributor.benchmark.classify import classify_outcome
from contributor.benchmark.llm_triage import llm_assess
from contributor.benchmark.models import (
    BenchmarkConfig,
    BenchmarkRecord,
    BenchmarkReport,
    CandidateInfo,
)
from contributor.benchmark.report import build_report, write_report
from contributor.benchmark.select import (
    SelectionThresholds,
    is_selectable,
    rejection_reasons,
    selection_reasons,
)
from contributor.discovery.github_search import DiscoveryFilters
from contributor.discovery.github_search import discover as discover_issues
from contributor.execution.git import diff_full
from contributor.execution.tests import TestRunner
from contributor.graph.nodes import WorkflowContext
from contributor.graph.workflow import run_to_completion
from contributor.models.state import EnvironmentStrategy, JobState, JobStatus, TestResult
from contributor.opencode.preflight import any_usable, run_all_preflights
from contributor.opencode.runner import OpenCodeRunner
from contributor.persistence.database import new_job_id
from contributor.persistence.usage import list_usage

_TERMINAL_ENV = {
    JobStatus.REPOSITORY_ENVIRONMENT_INCOMPATIBLE,
    JobStatus.ENVIRONMENT_FAILURE,
    JobStatus.BLOCKED_PROVIDER,
}


@dataclass
class _Candidate:
    number: int
    title: str
    body: str
    url: str
    labels: list[str]
    raw: dict[str, Any]


# Explicit priority labels from the benchmark spec (order the triage queue,
# never a subjective "best issue" ranking).
_PRIORITY_LABELS: tuple[tuple[str, int], ...] = (
    ("bug", 5),
    ("good first issue", 4),
    ("good-first-issue", 4),
    ("help wanted", 3),
    ("help-wanted", 3),
    ("testing", 2),
    ("test", 2),
    ("enhancement", 1),
)


def _priority_score(labels: list[str]) -> int:
    norm = {l.strip().lower() for l in labels}
    return sum(weight for label, weight in _PRIORITY_LABELS if label in norm)


def fetch_candidates(ctx: WorkflowContext, cfg: BenchmarkConfig) -> list[_Candidate]:
    """Retrieve a larger-than-needed pool of open issues for the repo.

    Candidates are stably ordered by explicit priority labels so the (costly)
    triage pass reaches concrete bugs before vague feature proposals.
    """
    f = DiscoveryFilters(repo=cfg.repo, label=list(cfg.labels), per_page=cfg.pool_size)
    try:
        items = discover_issues(ctx.github, f)
    except Exception:
        items = []
    out: list[_Candidate] = []
    for it in items:
        repo_url = str(it.get("repository_url", ""))
        full = repo_url.split("/repos/")[-1] if "/repos/" in repo_url else cfg.repo
        if full.lower() != cfg.repo.lower():
            continue
        num = int(it.get("number", 0) or 0)
        if not num:
            continue
        out.append(
            _Candidate(
                number=num,
                title=str(it.get("title", "")),
                body=str(it.get("body") or ""),
                url=str(it.get("html_url", "")),
                labels=[l.get("name", "") for l in it.get("labels", []) if isinstance(l, dict)],
                raw=it,
            )
        )
    out.sort(key=lambda c: -_priority_score(c.labels))  # stable: preserves search order
    return out


def fetch_specific(ctx: WorkflowContext, repo: str, numbers: list[int]) -> list[_Candidate]:
    """Fetch specific issues by number (for re-validating known candidates)."""
    out: list[_Candidate] = []
    for num in numbers:
        try:
            data = ctx.github.get_issue(*repo.split("/", 1), num)
        except Exception:
            continue
        out.append(
            _Candidate(
                number=num,
                title=str(data.get("title", "")),
                body=str(data.get("body") or ""),
                url=str(data.get("html_url", "")),
                labels=[l.get("name", "") for l in data.get("labels", []) if isinstance(l, dict)],
                raw=data,
            )
        )
    return out


def triage_pool(
    ctx: WorkflowContext,
    cfg: BenchmarkConfig,
    candidates: list[_Candidate],
    workdir: Path,
) -> tuple[list[CandidateInfo], list[tuple[_Candidate, Any, Any, CandidateInfo]]]:
    """Structured triage over the pool.

    1. Programmatic safety blocklist + deterministic assessment (fast reject).
    2. For candidates still eligible, a CHEAP-model pass inspects the body and
       can reject feature/design proposals and vague or security issues.
    """
    thresholds = SelectionThresholds(max_complexity=cfg.max_complexity)
    infos: list[CandidateInfo] = []
    eligible: list[tuple[_Candidate, Any, Any, CandidateInfo]] = []
    llm_calls = 0
    # Budget scales with the requested count so a benchmark of N can be filled.
    cap = max(cfg.triage_cap, cfg.count * 8)
    for c in candidates:
        tr = triage_issue(c.title, c.body, labels=c.labels)
        assessment = assess_solvability(c.title, c.body, labels=c.labels)
        reasons = rejection_reasons(assessment, tr, c.labels, thresholds)
        source = "deterministic"
        triage_model = ""
        triage_reason = ""
        if not reasons and cfg.llm_triage and llm_calls < cap:
            llm_calls += 1
            res = llm_assess(
                ctx.settings,
                ctx.runner,
                title=c.title,
                body=c.body,
                labels=c.labels,
                workdir=str(workdir),
                timeout=cfg.triage_timeout_s,
            )
            if res.ok:
                assessment = res.assessment
                source = "llm"
                triage_model = res.model
                triage_reason = res.reason
                reasons = rejection_reasons(assessment, tr, c.labels, thresholds)
                if res.decision != "accept":
                    reasons.append(f"llm:{res.decision}")
            else:
                triage_reason = f"llm_failed:{res.error}"
        info = CandidateInfo(
            number=c.number,
            title=c.title,
            url=c.url,
            labels=c.labels,
            assessment=assessment,
            triage_decision=tr.decision.value,
            triage_source=source,
            triage_model=triage_model,
            triage_reason=triage_reason,
            selected=False,
            rejection_reasons=reasons,
        )
        infos.append(info)
        if is_selectable(assessment, tr, c.labels, thresholds):
            eligible.append((c, tr, assessment, info))
    return infos, eligible


def select_with_environment(
    ctx: WorkflowContext,
    cfg: BenchmarkConfig,
    eligible: list[tuple[_Candidate, Any, Any, CandidateInfo]],
    count: int,
) -> list[tuple[_Candidate, Any, Any, CandidateInfo]]:
    """Run environment discovery on triage-passing candidates; keep compatible N."""
    owner, repo = cfg.repo.split("/", 1)
    try:
        meta = ctx.github.get_repo(owner, repo)
        default_branch = meta.get("default_branch", "main")
    except Exception:
        default_branch = "HEAD"
    thresholds = SelectionThresholds(max_complexity=cfg.max_complexity)
    selected: list[tuple[_Candidate, Any, Any, CandidateInfo]] = []
    for c, tr, assessment, info in eligible:
        if len(selected) >= count:
            break
        try:
            report = discover_environment_from_github(
                ctx.github, owner, repo, default_branch, issue_body=c.body
            )
        except Exception as e:
            info.rejection_reasons.append(f"env_discovery_failed:{e}")
            continue
        info.environment_strategy = report.strategy.value
        info.container_compatible = report.container_compatible
        if (
            not report.container_compatible
            or report.strategy in (EnvironmentStrategy.INCOMPATIBLE, EnvironmentStrategy.HOST_REQUIRED)
        ):
            info.rejection_reasons.append(f"env:{report.strategy.value}")
            continue
        info.selected = True
        info.selection_reasons = selection_reasons(assessment, thresholds)
        selected.append((c, tr, assessment, info))
    return selected


def _worker_ctx(base: WorkflowContext) -> WorkflowContext:
    from contributor.sandbox.session import SandboxSessionRegistry

    return WorkflowContext(
        settings=base.settings,
        db=base.db,
        github=base.github,
        sandbox=base.sandbox,
        runner=OpenCodeRunner(base.settings),
        benchmark_mode=True,
        sessions=SandboxSessionRegistry(base.settings),
    )


def _parse_model_label(label: str) -> tuple[str, str]:
    if " (variant=" in label and label.endswith(")"):
        model, _, rest = label.partition(" (variant=")
        return model, rest[:-1]
    return label, ""


def _test_dict(tr: TestResult | None) -> dict[str, Any]:
    if tr is None:
        return {}
    return {
        "command": tr.command,
        "passed": tr.passed,
        "exit_code": tr.exit_code,
        "environment_related": tr.environment_related,
        "timed_out": tr.timed_out,
        "failures": tr.failures[:5],
    }


def _run_broader_suite(
    ctx: WorkflowContext,
    cfg: BenchmarkConfig,
    final: JobState,
    targeted: TestResult | None,
) -> tuple[dict[str, Any], str]:
    """Run the repository's canonical suite after the targeted gate."""
    ws = Path(final.workspace_path) if final.workspace_path else None
    if targeted is None or not targeted.passed or ws is None or not ws.exists():
        return {}, "not_run"
    # If node_test could not derive a targeted command it already ran the
    # canonical suite; reuse that result instead of running it twice.
    if not final.issue_metadata.get("targeted_test_command"):
        result = "pass" if targeted.passed else (
            "environment_failure" if targeted.environment_related else "code_failure"
        )
        return _test_dict(targeted), result
    if not cfg.full_suite or final.current_state in _TERMINAL_ENV:
        return {}, "not_run"
    suggested = list(final.issue_metadata.get("env_test_commands") or [])
    if final.plan:
        for cmd in final.plan.commands_to_run:
            if cmd not in suggested:
                suggested.append(cmd)
    # Use the repo's primary ecosystem command, not an unrelated runner that
    # merely happens to exist in the checkout.
    from contributor.execution.tests import prefer_ecosystem_command

    primary = prefer_ecosystem_command(ws, suggested)
    suggested = [primary] if primary else suggested
    session = ctx.sessions.get(final.job_id) if ctx.sessions is not None else None
    try:
        tr = TestRunner(
            ctx.sandbox, test_timeout_s=ctx.settings.test_timeout_s, session=session
        ).run(ws, suggested=suggested)
    except Exception as e:
        return {"command": "(broader error)", "passed": False, "error": str(e)[:200]}, "code_failure"
    if tr.passed:
        result = "pass"
    elif tr.timed_out:
        result = "timeout"
    elif tr.environment_related:
        result = "environment_failure"
    else:
        result = "code_failure"
    return _test_dict(tr), result


def run_one(
    base: WorkflowContext,
    cfg: BenchmarkConfig,
    candidate: _Candidate,
    pre_triage: Any,
    assessment: Any,
    info: CandidateInfo,
) -> BenchmarkRecord:
    ctx = _worker_ctx(base)
    job = JobState(job_id=new_job_id(), repository=cfg.repo, issue_number=candidate.number)
    job.issue_title = candidate.title
    job.issue_body = candidate.body
    job.issue_metadata = {
        "labels": candidate.labels,
        "url": candidate.url,
        "benchmark": True,
    }
    ctx.repo.save_incremental(job)

    start = time.monotonic()
    try:
        final = run_to_completion(ctx, job, max_steps=60)
    except Exception as e:
        final = ctx.repo.get(job.job_id) or job
        final.errors.append(f"benchmark run failed: {e}")
    duration = time.monotonic() - start

    targeted = final.test_results[-1] if final.test_results else None
    broader, full_suite_result = _run_broader_suite(ctx, cfg, final, targeted)

    ws = Path(final.workspace_path) if final.workspace_path else None
    diff_path = ""
    diff_lines = 0
    if ws is not None and ws.exists():
        diff = diff_full(ws)
        diff_lines = len(diff.splitlines())
        if diff.strip():
            diff_dir = Path(cfg.output_dir) / "diffs"
            diff_dir.mkdir(parents=True, exist_ok=True)
            dp = diff_dir / f"issue-{candidate.number}.diff"
            dp.write_text(diff)
            diff_path = str(dp)

    triage_res = final.triage_result or pre_triage
    env = final.environment_report
    review = final.review_result
    boot = final.bootstrap_report or {}
    health = final.environment_health or {}
    tier = final.model_selections.get("tier", "")
    model, variant = _parse_model_label(final.model_selections.get("implement", ""))
    usage = list_usage(base.db, job.job_id)

    return BenchmarkRecord(
        repository=cfg.repo,
        issue_number=candidate.number,
        issue_title=final.issue_title or candidate.title,
        issue_url=candidate.url,
        job_id=job.job_id,
        selected=True,
        selection_reasons=info.selection_reasons,
        triage={
            "decision": triage_res.decision.value if triage_res else "unknown",
            "issue_type": triage_res.issue_type if triage_res else "unknown",
            "difficulty": triage_res.difficulty if triage_res else 0,
            "confidence": triage_res.confidence if triage_res else 0.0,
            "reason": triage_res.reason if triage_res else "",
            "signals": assessment.signals,
        },
        assessment=assessment,
        human_baseline={
            "clear_reproduction": assessment.clear_reproduction,
            "clear_expected_behavior": assessment.clear_expected_behavior,
            "existing_relevant_tests": assessment.existing_relevant_tests,
            "clear_location_in_code": assessment.clear_location_in_code,
            "obvious_acceptance_condition": assessment.obvious_acceptance_condition,
        },
        environment={
            "strategy": env.strategy.value if env else info.environment_strategy,
            "container_compatible": env.container_compatible if env else info.container_compatible,
            "languages": env.languages if env else [],
            "test_commands": env.test_commands if env else [],
            "signals": env.signals if env else [],
        },
        complexity=assessment.estimated_complexity,
        model_tier=tier,
        model=model,
        variant=variant,
        implementation_attempts=final.implementation_attempt,
        debug_attempts=final.debug_attempt,
        review_cycles=final.review_attempt,
        targeted_tests=_test_dict(targeted),
        broader_tests=broader,
        review={
            "verdict": review.verdict.value if review else "",
            "summary": review.summary if review else "",
        },
        full_suite_result=full_suite_result,
        duration_s=round(duration, 1),
        environment_bootstrap_time_s=float(boot.get("duration_s") or 0.0),
        environment_cache_hit=bool(final.issue_metadata.get("env_cache_hit")),
        resource_profile=final.resource_profile,
        toolchain_versions=health.get("toolchain_versions") or {},
        bootstrap_commands=boot.get("commands") or [],
        model_usage=[dict(r) for r in usage],
        estimated_cost=None,
        outcome=classify_outcome(final, full_suite_result=full_suite_result),
        workspace_path=final.workspace_path,
        diff_path=diff_path,
        diff_lines=diff_lines,
        errors=final.errors[-10:],
    )


def run_benchmark(
    ctx: WorkflowContext,
    cfg: BenchmarkConfig,
    *,
    progress: Callable[[BenchmarkRecord], None] | None = None,
) -> BenchmarkReport:
    """Discover, triage, select and execute a benchmark. Never pushes or opens PRs."""
    if cfg.preflight and not cfg.dry_run:
        results = run_all_preflights(ctx.settings, ctx.runner, health_store=ctx.health)
        if not any_usable(results):
            detail = "; ".join(f"{t}:{r.error_kind or 'unavailable'}" for t, r in results.items())
            raise RuntimeError(f"BLOCKED_PROVIDER: no usable OpenCode model. {detail}")

    candidates = fetch_specific(ctx, cfg.repo, cfg.issues) if cfg.issues else fetch_candidates(ctx, cfg)
    triage_dir = Path(tempfile.mkdtemp(prefix="bench-triage-"))
    infos, eligible = triage_pool(ctx, cfg, candidates, triage_dir)
    if cfg.issues:
        # Explicitly requested issues bypass selection filters (re-validation).
        info_by_num = {i.number: i for i in infos}
        selected = []
        for c in candidates:
            info = info_by_num[c.number]
            info.selected = True
            info.selection_reasons = ["explicitly_requested"]
            tr = triage_issue(c.title, c.body, labels=c.labels)
            selected.append((c, tr, info.assessment, info))
    else:
        selected = select_with_environment(ctx, cfg, eligible, cfg.count)

    if cfg.dry_run:
        # Selection-only: exercise discovery/triage/environment without executing.
        report = build_report(cfg, infos, [])
        write_report(report, cfg.output_dir)
        return report

    records: list[BenchmarkRecord] = []
    if cfg.workers <= 1:
        for c, tr, assessment, info in selected:
            rec = run_one(ctx, cfg, c, tr, assessment, info)
            records.append(rec)
            # Persist incrementally so a long/partial run still yields a report.
            write_report(build_report(cfg, infos, records), cfg.output_dir)
            if progress:
                progress(rec)
    else:
        with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
            futures = [
                pool.submit(run_one, ctx, cfg, c, tr, assessment, info)
                for c, tr, assessment, info in selected
            ]
            for fut in as_completed(futures):
                rec = fut.result()
                records.append(rec)
                if progress:
                    progress(rec)
    records.sort(key=lambda r: r.issue_number)

    report = build_report(cfg, infos, records)
    write_report(report, cfg.output_dir)
    return report
