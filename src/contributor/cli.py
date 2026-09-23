"""CLI: issue/discover/run/status/logs/resume/cancel/config/autonomous."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from contributor.config import get_settings
from contributor.discovery.github_search import DiscoveryFilters
from contributor.discovery.github_search import discover as discover_issues
from contributor.github.client import GitHubClient
from contributor.graph.nodes import WorkflowContext
from contributor.graph.workflow import run_to_completion
from contributor.models.state import IssueRef, JobState, JobStatus
from contributor.observability.logging import setup_logging
from contributor.opencode.runner import OpenCodeRunner
from contributor.persistence.database import Database, new_job_id
from contributor.persistence.repositories import JobRepository
from contributor.sandbox.manager import SandboxManager

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


def make_context(
    *,
    execution_mode: str = "benchmark",
    allow_push: bool = False,
    allow_create_pr: bool = False,
    ci_monitor: bool = False,
    auto_merge: bool = False,
    push_repo: str = "",
    push_remote: str = "origin",
) -> WorkflowContext:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)
    db = Database(settings.database_url)
    github = GitHubClient(token=settings.github_token, api_base=settings.github_api_base, timeout=settings.github_timeout_s)
    sandbox = SandboxManager(settings)
    runner = OpenCodeRunner(settings)
    from contributor.sandbox.session import SandboxSessionRegistry

    return WorkflowContext(
        settings=settings, db=db, github=github, sandbox=sandbox, runner=runner,
        sessions=SandboxSessionRegistry(settings),
        execution_mode=execution_mode, allow_push=allow_push, allow_create_pr=allow_create_pr,
        ci_monitor=ci_monitor, auto_merge=auto_merge,
        push_repo=push_repo, push_remote=push_remote,
    )


def _create_job(ctx: WorkflowContext, ref: IssueRef) -> JobState:
    job = JobState(job_id=new_job_id(), repository=ref.full_name, issue_number=ref.number)
    job.execution_mode = ctx.execution_mode
    job.allow_push = ctx.allow_push
    job.allow_create_pr = ctx.allow_create_pr
    job.ci_monitor = ctx.ci_monitor
    job.auto_merge = ctx.auto_merge
    job.add_event("JOB_CREATED", f"Created for {ref.short} mode={ctx.execution_mode}")
    ctx.repo.save_incremental(job)
    return job


def _preflight_gate(ctx: WorkflowContext, job: JobState) -> bool:
    """Preflight every tier BEFORE cloning. Proceed if any model is usable;
    otherwise mark BLOCKED_PROVIDER. Unavailable models are recorded."""
    from contributor.models.state import JobStatus
    from contributor.observability import events
    from contributor.observability.events import emit
    from contributor.opencode.preflight import any_usable, run_all_preflights

    job.current_state = JobStatus.PREPARE_WORKSPACE
    emit(job, events.PREFLIGHT_STARTED, "OpenCode preflight (all tiers)", agent="preflight")
    results = run_all_preflights(ctx.settings, ctx.runner, health_store=ctx.health)
    usable = [t for t, r in results.items() if r.ok]
    unavailable = {t: r.error_kind for t, r in results.items() if not r.ok}
    if any_usable(results):
        emit(job, events.PREFLIGHT_PASSED, f"usable tiers: {', '.join(usable)}", agent="preflight",
             data={"usable": usable, "unavailable": unavailable})
        ctx.repo.save_incremental(job)
        return True
    detail = "; ".join(f"{t}:{r.error_kind or r.detail[:60]}" for t, r in results.items())
    job.current_state = JobStatus.BLOCKED_PROVIDER
    job.escalated = True
    job.human_escalation_reason = f"No usable OpenCode model. {detail}"
    job.errors.append(job.human_escalation_reason)
    emit(job, events.PREFLIGHT_FAILED, job.human_escalation_reason, agent="preflight")
    job.done = True
    ctx.repo.save_incremental(job)
    console.print(f"[red]BLOCKED_PROVIDER: {job.human_escalation_reason}[/red]")
    return False


@app.command()
def issue(ref: str, dry_run: bool = typer.Option(False, help="Only fetch + triage, do not implement")):
    """Fetch an issue, triage it, and (by default) run the full workflow."""
    ctx = make_context()
    issue_ref = IssueRef.parse(ref)
    job = _create_job(ctx, issue_ref)
    console.print(f"[bold]Job {job.job_id}[/bold] for {issue_ref.short}")
    if dry_run:
        from contributor.github.issues import normalize_issue
        from contributor.agents.triage import triage_issue

        data = ctx.github.get_issue(issue_ref.owner, issue_ref.repo, issue_ref.number)
        title, body, meta = normalize_issue(data)
        tr = triage_issue(title, body, labels=meta.get("labels", []))
        console.print(tr.model_dump_json(indent=2))
        return
    if not _preflight_gate(ctx, job):
        raise typer.Exit(2)
    final = run_to_completion(ctx, job)
    console.print(f"Done: state={final.current_state.value} pr={final.pull_request_url or '-'} escalated={final.escalated}")


@app.command()
def run(
    ref: str,
    push: bool = typer.Option(False, "--push/--no-push", help="Live mode: allow orchestrator push"),
    create_pr: bool = typer.Option(False, "--pr/--no-pr", help="Live mode: also open a PR (default off)"),
    ci: bool = typer.Option(False, "--ci/--no-ci", help="Live PR mode: monitor CI checks"),
    push_repo: str = typer.Option("", "--push-repo", help="Push target OWNER/REPO (default: issue repo)"),
    push_remote: str = typer.Option("origin", "--push-remote", help="Remote name label for the push target"),
):
    """Run the autonomous workflow for OWNER/REPO#123.

    Without --push this is non-destructive (no GitHub mutation).
    """
    mode = "live" if push else "benchmark"
    ctx = make_context(
        execution_mode=mode, allow_push=push, allow_create_pr=create_pr,
        ci_monitor=bool(create_pr and ci),
        push_repo=push_repo, push_remote=push_remote,
    )
    issue_ref = IssueRef.parse(ref)
    job = _create_job(ctx, issue_ref)
    console.print(
        f"[bold]Job {job.job_id}[/bold] for {issue_ref.short} "
        f"mode={mode} push={push} pr={create_pr} ci={bool(create_pr and ci)} "
        f"target={push_repo or issue_ref.full_name}"
    )
    if not _preflight_gate(ctx, job):
        raise typer.Exit(2)
    final = run_to_completion(ctx, job)
    console.print(
        f"Done: state={final.current_state.value} pr={final.pull_request_url or '-'} "
        f"pushed={bool(final.push_result and final.push_result.get('ok'))} "
        f"target={final.push_target or '-'} escalated={final.escalated}"
    )


@app.command()
def live(
    ref: str,
    push_repo: str = typer.Option("", "--push-repo", help="Push target OWNER/REPO (e.g. fork)"),
    push_remote: str = typer.Option("origin", "--push-remote"),
    create_pr: bool = typer.Option(False, "--pr/--no-pr"),
    ci: bool = typer.Option(True, "--ci/--no-ci", help="Monitor CI after opening the PR"),
):
    """Live contribution: implement, verify, push, and (optionally) open a PR.

    With --pr the orchestrator opens a PR and (by default) monitors CI. The
    system never merges automatically.
    """
    ctx = make_context(
        execution_mode="live", allow_push=True, allow_create_pr=create_pr,
        ci_monitor=bool(create_pr and ci),
        push_repo=push_repo, push_remote=push_remote,
    )
    issue_ref = IssueRef.parse(ref)
    job = _create_job(ctx, issue_ref)
    console.print(
        f"[bold]LIVE[/bold] job={job.job_id} issue={issue_ref.short} "
        f"pr={create_pr} ci={bool(create_pr and ci)} target={push_repo or issue_ref.full_name}"
    )
    if not _preflight_gate(ctx, job):
        raise typer.Exit(2)
    final = run_to_completion(ctx, job)
    console.print(
        f"Done: state={final.current_state.value} pushed="
        f"{bool(final.push_result and final.push_result.get('ok'))} "
        f"pr={final.pull_request_url or '-'} "
        f"branch={final.branch_name or '-'} sha={final.commit_sha[:12] or '-'}"
    )


@app.command()
def discover(
    language: str = typer.Option("", help="Filter by language"),
    label: list[str] = typer.Option([], help="Repeatable label filter"),
    repo: str = typer.Option("", help="Restrict to OWNER/REPO"),
    min_stars: int = typer.Option(0),
    keyword: str = typer.Option(""),
    unassigned: bool = typer.Option(False, "--unassigned", help="Only unassigned issues"),
    max_difficulty: int = typer.Option(5, "--max-difficulty", help="Advisory difficulty ceiling"),
    max_age: int = typer.Option(0, "--max-age", help="Only issues newer than N days (0=any)"),
    max_results: int = typer.Option(20),
):
    """Search GitHub for candidate issues (read-only, never modifies repos)."""
    ctx = make_context()
    f = DiscoveryFilters(language=language, label=list(label), repo=repo, min_stars=min_stars,
                         keyword=keyword, unassigned_only=unassigned, per_page=max_results,
                         max_difficulty=max_difficulty, max_age_days=max_age)
    items = discover_issues(ctx.github, f)
    t = Table(title=f"Candidates ({len(items)})")
    t.add_column("Issue"); t.add_column("Title"); t.add_column("Labels")
    for it in items:
        repo_url = (it.get("repository_url") or "")
        rn = repo_url.split("/repos/")[-1] if "/repos/" in repo_url else "?"
        t.add_row(f"{rn}#{it.get('number')}", str(it.get("title", ""))[:70],
                  ",".join(l.get("name", "") for l in it.get("labels", [])))
    console.print(t)


@app.command()
def status(job_id: str):
    ctx = make_context()
    st = ctx.repo.get(job_id)
    if not st:
        console.print(f"[red]Job {job_id} not found[/red]")
        raise typer.Exit(1)
    console.print(f"job={st.job_id} repo={st.repository}#{st.issue_number} state={st.current_state.value} "
                  f"impl={st.implementation_attempt} debug={st.debug_attempt} review={st.review_attempt} "
                  f"pr={st.pull_request_url or '-'} escalated={st.escalated} done={st.done}")
    if st.human_escalation_reason:
        console.print(f"[yellow]escalation: {st.human_escalation_reason}[/yellow]")
    if st.errors:
        console.print("[red]errors:[/red]")
        for e in st.errors[-5:]:
            console.print(f" - {e[:300]}")


@app.command()
def logs(job_id: str, limit: int = typer.Option(50)):
    ctx = make_context()
    evs = ctx.repo.events(job_id)
    if not evs:
        st = ctx.repo.get(job_id)
        evs = st.event_history if st else []
    for e in evs[-limit:]:
        console.print(f"{e.at} {e.type} agent={e.agent or '-'} attempt={e.attempt} {e.message[:200]}")


@app.command()
def resume(job_id: str):
    """Resume a job after crash/restart from persisted state.

    Reconstructs the live push/PR/CI mode from the persisted job so an
    interrupted PR or CI monitor can continue without opening a second PR.
    """
    probe = make_context()
    st = probe.repo.get(job_id)
    if not st:
        console.print(f"[red]Job {job_id} not found[/red]")
        raise typer.Exit(1)
    if st.done:
        console.print(f"Job already done: {st.current_state.value}")
        return
    ctx = make_context(
        execution_mode=st.execution_mode,
        allow_push=st.allow_push,
        allow_create_pr=st.allow_create_pr,
        ci_monitor=st.ci_monitor,
        auto_merge=st.auto_merge,
        push_repo=st.push_target or "",
        push_remote=st.push_remote or "origin",
    )
    from contributor.graph.workflow import resume_entry

    entry = resume_entry(st)
    console.print(
        f"Resuming job {job_id} from {st.current_state.value} (entry={entry}) "
        f"mode={st.execution_mode} pr={st.allow_create_pr} ci={st.ci_monitor}"
    )
    final = run_to_completion(ctx, st, entry=entry)
    console.print(
        f"Done: state={final.current_state.value} pr={final.pull_request_url or '-'} "
        f"ci={final.ci_result.state if final.ci_result else '-'}"
    )


@app.command()
def cancel(job_id: str):
    ctx = make_context()
    st = ctx.repo.get(job_id)
    if not st:
        console.print(f"[red]Job {job_id} not found[/red]")
        raise typer.Exit(1)
    st.current_state = JobStatus.CANCELLED
    st.done = True
    st.add_event("JOB_FAILED", "Cancelled by operator")
    ctx.repo.save_incremental(st)
    try:
        ctx.runner.cancel()
    except Exception:
        pass
    console.print(f"Cancelled {job_id}")


@app.command()
def config():
    s = get_settings()
    console.print(s.model_dump_json(indent=2))


@app.command()
def usage(job_id: str = typer.Option("", "--job", help="Limit to one job")):
    """Show model usage accounting (tier, model, duration, outcome)."""
    import json as _json

    from contributor.persistence.usage import list_usage, summarize_usage

    ctx = make_context()
    rows = list_usage(ctx.db, job_id or None)
    t = Table(title=f"Model usage ({len(rows)} calls)")
    for col in ("job", "task", "tier", "model", "variant", "seconds", "outcome"):
        t.add_column(col)
    for r in rows[:100]:
        t.add_row(r.get("job_id", "")[:10], r.get("task", ""), r.get("tier", ""),
                  r.get("model", ""), r.get("variant", "") or "-",
                  f"{r.get('duration_s', 0):.1f}", r.get("outcome", ""))
    console.print(t)
    console.print(_json.dumps(summarize_usage(ctx.db)))


@app.command()
def metrics(job_id: str = typer.Option("", "--job", help="Limit to one job")):
    """PR/CI lifecycle metrics (rates are deterministic, from persisted facts)."""
    from contributor.observability.metrics import aggregate_metrics, job_metrics
    from contributor.persistence.usage import list_usage

    ctx = make_context()
    jobs = [ctx.repo.get(job_id)] if job_id else ctx.repo.list(limit=200)
    jobs = [j for j in jobs if j is not None]
    rows = [job_metrics(j, list_usage(ctx.db, j.job_id)) for j in jobs]
    console.print_json(data=(rows[0] if (job_id and rows) else aggregate_metrics(rows)))


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
    no_probe: bool = typer.Option(False, "--no-probe", help="Skip live inference probe"),
):
    """Diagnose the OpenCode integration (binary, auth, models, network, sandbox)."""
    import json as _json

    from contributor.opencode.doctor import run_doctor

    ctx = make_context()
    rep = run_doctor(ctx.settings, probe_inference_enabled=not no_probe, health_store=ctx.health)
    if json_output:
        console.print(_json.dumps({
            "ok": rep.ok,
            "sections": rep.sections,
            "flags": rep.flags,
            "usable_models": rep.usable_models,
            "unavailable_models": rep.unavailable_models,
            "warnings": rep.warnings,
        }, indent=2))
    else:
        for section, items in rep.sections.items():
            console.print(f"[bold]{section}[/bold]")
            for name, status in items.items():
                mark = "[green]OK[/green]" if rep.is_ok(section, name) else "[red]FAIL[/red]"
                console.print(f"  {mark} {name}: {status[:160]}")
        console.print("[green]doctor: healthy[/green]" if rep.ok else "[red]doctor: problems found[/red]")
    if not rep.ok:
        raise typer.Exit(1)


@app.command()
def benchmark(
    repo: str = typer.Option(..., "--repo", help="OWNER/REPO to benchmark"),
    count: int = typer.Option(5, "--count", help="Number of issues to attempt"),
    workers: int = typer.Option(1, "--workers", help="Concurrent issue workers"),
    label: list[str] = typer.Option([], "--label", help="Repeatable label filter"),
    issue: list[int] = typer.Option([], "--issue", help="Force specific issue numbers (repeatable)"),
    max_complexity: int = typer.Option(3, "--max-complexity", help="Max estimated complexity (1-5)"),
    pool_size: int = typer.Option(100, "--pool-size", help="Candidate pool size before selection"),
    out: str = typer.Option(".", "--out", help="Output dir for benchmark-report.{json,md} and diffs"),
    full_suite: bool = typer.Option(True, "--full-suite/--no-full-suite", help="Run the broader suite after targeted tests"),
    no_preflight: bool = typer.Option(False, "--no-preflight", help="Skip OpenCode preflight probes"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Select issues only; do not execute"),
    no_llm_triage: bool = typer.Option(False, "--no-llm-triage", help="Use only deterministic triage (no cheap-model pass)"),
    push: bool = typer.Option(False, "--push", help="(disabled) allow pushing branches"),
    create_pr: bool = typer.Option(False, "--pr", help="(disabled) allow opening PRs"),
):
    """Benchmark the contributor against a real repository (non-destructive by default).

    Discovery -> structured triage -> environment discovery -> transparent
    selection -> execute -> targeted tests -> broader tests -> review.
    Never pushes branches or opens PRs.
    """
    if push or create_pr:
        console.print(
            "[red]benchmark mode is non-destructive: pushing branches / opening PRs is "
            "not supported here. Use `contributor run` for the PR workflow.[/red]"
        )
        raise typer.Exit(2)

    from contributor.benchmark.models import BenchmarkConfig
    from contributor.benchmark.runner import run_benchmark

    cfg = BenchmarkConfig(
        repo=repo,
        count=max(1, count),
        workers=max(1, workers),
        labels=list(label),
        issues=list(issue),
        max_complexity=max_complexity,
        pool_size=max(pool_size, count),
        output_dir=out,
        full_suite=full_suite,
        no_push=True,
        no_pr=True,
        preflight=not no_preflight,
        dry_run=dry_run,
        llm_triage=not no_llm_triage,
    )
    ctx = make_context()
    console.print(
        f"[bold]Benchmark[/bold] {cfg.repo} count={cfg.count} workers={cfg.workers} "
        f"labels={cfg.labels or '(none)'} max_complexity={cfg.max_complexity}"
    )

    def _progress(rec) -> None:
        console.print(
            f"  #{rec.issue_number} -> [bold]{rec.outcome.value}[/bold] "
            f"({rec.duration_s:.0f}s, impl={rec.implementation_attempts}, "
            f"debug={rec.debug_attempts}, tests={rec.targeted_tests.get('passed')})"
        )

    try:
        report = run_benchmark(ctx, cfg, progress=_progress)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2)

    s = report.summary
    console.print(
        f"\n[green]attempted={s.issues_attempted} success={s.successful} "
        f"success_after_repair={s.successful_after_repair} "
        f"test_failures={s.test_failures} env={s.environment_failures} "
        f"provider={s.provider_failures} human={s.human_escalations}[/green]"
    )
    console.print(f"reports: {cfg.output_dir}/benchmark-report.json, {cfg.output_dir}/benchmark-report.md")


@app.command()
def autonomous(
    language: str = typer.Option("", help="Discovery language filter"),
    label: list[str] = typer.Option([]),
    max_jobs: int = typer.Option(0, help="0 = run forever"),
    once: bool = typer.Option(False, help="Single discovery pass"),
):
    """Continuously discover -> triage -> execute with concurrency cap."""
    from contributor.opencode.preflight import any_usable, run_all_preflights

    ctx = make_context()
    settings = ctx.settings
    results = run_all_preflights(settings, ctx.runner, health_store=ctx.health)
    usable = [t for t, r in results.items() if r.ok]
    if not any_usable(results):
        detail = "; ".join(f"{t}:{r.error_kind or 'unavailable'}" for t, r in results.items())
        console.print(f"[red]BLOCKED_PROVIDER, autonomous not started: {detail}[/red]")
        raise typer.Exit(2)
    console.print(f"[green]preflight OK, usable tiers: {', '.join(usable)}[/green]")
    completed = 0
    with ThreadPoolExecutor(max_workers=settings.max_concurrent_jobs) as pool:
        while True:
            f = DiscoveryFilters(language=language, label=list(label), per_page=10)
            try:
                items = discover_issues(ctx.github, f)
            except Exception as e:
                console.print(f"[red]discovery failed: {e}[/red]")
                items = []
            futures = []
            for it in items:
                try:
                    num = int(it.get("number", 0))
                    repo_url = it.get("repository_url", "")
                    full = repo_url.split("/repos/")[-1] if "/repos/" in repo_url else ""
                    if not full or not num:
                        continue
                    owner, repo = full.split("/", 1)
                    ref = IssueRef(owner=owner, repo=repo, number=num)
                    job = _create_job(ctx, ref)
                    futures.append(pool.submit(run_to_completion, ctx, job))
                    if max_jobs and completed + len(futures) >= max_jobs:
                        break
                except Exception as e:
                    console.print(f"[red]queue failed: {e}[/red]")
            for fut in futures:
                try:
                    final = fut.result(timeout=settings.job_timeout_s)
                    console.print(f"job {final.job_id} -> {final.current_state.value}")
                except Exception as e:
                    console.print(f"[red]job failed: {e}[/red]")
                completed += 1
                if max_jobs and completed >= max_jobs:
                    console.print("max_jobs reached")
                    return
            if once:
                return
            console.print(f"poll sleep {settings.autonomous_poll_interval_s}s ...")
            time.sleep(settings.autonomous_poll_interval_s)


if __name__ == "__main__":
    app()
