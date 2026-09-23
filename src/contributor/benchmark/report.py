"""Benchmark reporting: aggregate metrics + JSON/Markdown artifacts."""
from __future__ import annotations

import json
import statistics
from pathlib import Path

from contributor.benchmark.models import (
    BenchmarkConfig,
    BenchmarkRecord,
    BenchmarkReport,
    BenchmarkSummary,
    CandidateInfo,
    Outcome,
)


def summarize(config: BenchmarkConfig, records: list[BenchmarkRecord]) -> BenchmarkSummary:
    n = len(records)
    counts: dict[Outcome, int] = {o: 0 for o in Outcome}
    for r in records:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1

    durations = [r.duration_s for r in records if r.duration_s > 0]
    impl = [r.implementation_attempts for r in records]
    dbg = [r.debug_attempts for r in records]

    def rate(k: int) -> float:
        return round(k / n, 4) if n else 0.0

    usage: dict[str, dict] = {}
    for r in records:
        for u in r.model_usage:
            tier = u.get("tier") or "unknown"
            slot = usage.setdefault(tier, {"calls": 0, "seconds": 0.0})
            slot["calls"] += 1
            slot["seconds"] = round(slot["seconds"] + float(u.get("duration_s") or 0), 1)

    env_failures = counts[Outcome.ENVIRONMENT_INCOMPATIBLE]
    res_failures = counts[Outcome.RESOURCE_INCOMPATIBLE]
    return BenchmarkSummary(
        repository=config.repo,
        issues_attempted=n,
        successful=counts[Outcome.SUCCESS],
        successful_after_repair=counts[Outcome.SUCCESS_AFTER_REPAIR],
        implementation_failures=counts[Outcome.IMPLEMENTATION_FAILED],
        test_failures=counts[Outcome.TEST_FAILURE],
        environment_failures=env_failures,
        resource_failures=res_failures,
        provider_failures=counts[Outcome.PROVIDER_BLOCKED],
        human_escalations=counts[Outcome.HUMAN_REQUIRED],
        triage_rejected=counts[Outcome.TRIAGE_REJECTED],
        timeouts=counts[Outcome.TIMEOUT],
        success_rate=rate(counts[Outcome.SUCCESS]),
        success_after_repair_rate=rate(counts[Outcome.SUCCESS_AFTER_REPAIR]),
        implementation_failure_rate=rate(counts[Outcome.IMPLEMENTATION_FAILED]),
        environment_incompatibility_rate=rate(env_failures),
        resource_incompatibility_rate=rate(res_failures),
        provider_failure_rate=rate(counts[Outcome.PROVIDER_BLOCKED]),
        average_duration_s=round(statistics.fmean(durations), 1) if durations else 0.0,
        median_duration_s=round(statistics.median(durations), 1) if durations else 0.0,
        average_implementation_attempts=round(statistics.fmean(impl), 2) if impl else 0.0,
        average_debug_attempts=round(statistics.fmean(dbg), 2) if dbg else 0.0,
        model_usage_by_tier=usage,
    )


def build_report(
    config: BenchmarkConfig,
    candidates: list[CandidateInfo],
    records: list[BenchmarkRecord],
) -> BenchmarkReport:
    return BenchmarkReport(
        config=config,
        summary=summarize(config, records),
        candidates=candidates,
        records=records,
    )


def write_report(report: BenchmarkReport, output_dir: str | Path = ".") -> tuple[Path, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "benchmark-report.json"
    md_path = out / "benchmark-report.md"
    json_path.write_text(json.dumps(report.model_dump(mode="json"), indent=2))
    md_path.write_text(render_markdown(report))
    return json_path, md_path


def render_markdown(report: BenchmarkReport) -> str:
    s = report.summary
    c = report.config
    lines: list[str] = []
    lines.append("# Benchmark report\n")
    lines.append(f"- Repository: `{c.repo}`")
    lines.append(f"- Requested count: {c.count} (workers={c.workers}, max_complexity={c.max_complexity})")
    lines.append(f"- Labels: {', '.join(c.labels) if c.labels else '(none)'}")
    lines.append(f"- Public side effects: push={not c.no_push}, pr={not c.no_pr}")
    lines.append(f"- Generated: {s.generated_at}\n")

    lines.append("## Summary\n")
    lines.append(f"- Repository: {s.repository}")
    lines.append(f"- Issues attempted: {s.issues_attempted}")
    lines.append(f"- Successful: {s.successful}")
    lines.append(f"- Successful after repair: {s.successful_after_repair}")
    lines.append(f"- Implementation failures: {s.implementation_failures}")
    lines.append(f"- Test failures: {s.test_failures}")
    lines.append(f"- Environment failures: {s.environment_failures}")
    lines.append(f"- Resource failures: {s.resource_failures}")
    lines.append(f"- Provider failures: {s.provider_failures}")
    lines.append(f"- Human escalations: {s.human_escalations}")
    lines.append(f"- Triage rejected: {s.triage_rejected}")
    lines.append(f"- Timeouts: {s.timeouts}\n")

    lines.append("## Per-issue results\n")
    for r in report.records:
        lines.append(f"### #{r.issue_number} — {r.issue_title}\n")
        lines.append(f"- Issue: https://github.com/{r.repository}/issues/{r.issue_number}")
        lines.append(f"- URL: {r.issue_url or '-'}")
        lines.append(f"- Job: `{r.job_id}`")
        tr = r.triage or {}
        lines.append(
            f"- Triage: decision={tr.get('decision','?')} type={tr.get('issue_type','?')} "
            f"difficulty={tr.get('difficulty','?')} | assessment complexity={r.complexity} "
            f"actionable={r.assessment.actionable} container_testable={r.assessment.container_testable}"
        )
        env = r.environment or {}
        lines.append(
            f"- Environment: strategy={env.get('strategy','?')} "
            f"container_compatible={env.get('container_compatible','?')} "
            f"languages={env.get('languages','?')}"
        )
        lines.append(
            f"- Env provisioning: profile={r.resource_profile or '-'} "
            f"bootstrap={r.environment_bootstrap_time_s:.0f}s cache_hit={r.environment_cache_hit} "
            f"toolchain={r.toolchain_versions or {}}"
        )
        lines.append(f"- Model: tier={r.model_tier or '-'} model={r.model or '-'} variant={r.variant or '-'}")
        lines.append(
            f"- Attempts: implementation={r.implementation_attempts} debug={r.debug_attempts} "
            f"review_cycles={r.review_cycles}"
        )
        tt = r.targeted_tests or {}
        bt = r.broader_tests or {}
        lines.append(
            f"- Tests: targeted=`{tt.get('command','(none)')}` passed={tt.get('passed','?')}; "
            f"broader=`{bt.get('command','(none)')}` passed={bt.get('passed','?')}; "
            f"full_suite={r.full_suite_result}"
        )
        rv = (r.review or {}).get("verdict", "?")
        lines.append(f"- Review: verdict={rv} summary={(r.review or {}).get('summary','')[:160]}")
        lines.append(f"- Duration: {r.duration_s:.1f}s")
        lines.append(f"- Diff: {r.diff_lines} lines ({r.diff_path or '-'})")
        lines.append(f"- Final result: **{r.outcome.value}**")
        if r.errors:
            lines.append("- Errors:")
            for e in r.errors[-3:]:
                lines.append(f"  - {e[:240]}")
        lines.append("")

    lines.append("## Aggregate metrics\n")
    lines.append(f"- Success rate: {s.success_rate:.1%}")
    lines.append(f"- Success-after-repair rate: {s.success_after_repair_rate:.1%}")
    lines.append(f"- Implementation failure rate: {s.implementation_failure_rate:.1%}")
    lines.append(f"- Environment incompatibility rate: {s.environment_incompatibility_rate:.1%}")
    lines.append(f"- Resource incompatibility rate: {s.resource_incompatibility_rate:.1%}")
    lines.append(f"- Provider failure rate: {s.provider_failure_rate:.1%}")
    lines.append(f"- Average duration: {s.average_duration_s:.1f}s")
    lines.append(f"- Median duration: {s.median_duration_s:.1f}s")
    lines.append(f"- Average implementation attempts: {s.average_implementation_attempts}")
    lines.append(f"- Average debug attempts: {s.average_debug_attempts}")
    lines.append("- Model usage by tier:")
    if s.model_usage_by_tier:
        for tier, u in sorted(s.model_usage_by_tier.items()):
            lines.append(f"  - {tier}: calls={u['calls']} seconds={u['seconds']}")
    else:
        lines.append("  - (none)")
    lines.append("")

    lines.append("## Candidate pool\n")
    for c in report.candidates:
        status = "SELECTED" if c.selected else "rejected"
        detail = ", ".join(c.selection_reasons if c.selected else c.rejection_reasons)
        lines.append(
            f"- #{c.number} [{status}] (triage={c.triage_source}) {c.title[:90]} — {detail}"
        )
    lines.append("")
    return "\n".join(lines)
