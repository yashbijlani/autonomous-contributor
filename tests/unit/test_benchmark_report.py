"""Unit: benchmark aggregation + report artifacts."""
from pathlib import Path

from contributor.benchmark.models import (
    BenchmarkConfig,
    BenchmarkRecord,
    CandidateInfo,
    Outcome,
)
from contributor.benchmark.report import build_report, render_markdown, summarize, write_report
from contributor.models.state import TriageAssessment


def _record(number: int, outcome: Outcome, duration: float, impl: int = 1, debug: int = 0) -> BenchmarkRecord:
    a = TriageAssessment(actionable=True, container_testable=True, estimated_complexity=2, confidence=0.8)
    return BenchmarkRecord(
        repository="o/r",
        issue_number=number,
        issue_title=f"Issue {number}",
        issue_url=f"https://github.com/o/r/issues/{number}",
        job_id=f"job{number}",
        selected=True,
        triage={"decision": "accept", "issue_type": "bug", "difficulty": 2},
        assessment=a,
        complexity=2,
        model_tier="strong",
        model="opencode-go/x",
        variant="high",
        implementation_attempts=impl,
        debug_attempts=debug,
        targeted_tests={"command": "pytest", "passed": True},
        review={"verdict": "approved", "summary": "ok"},
        duration_s=duration,
        model_usage=[{"tier": "strong", "duration_s": 10.0}, {"tier": "cheap", "duration_s": 2.0}],
        outcome=outcome,
    )


def test_summarize_counts_and_rates():
    cfg = BenchmarkConfig(repo="o/r", count=3)
    records = [
        _record(1, Outcome.SUCCESS, 10.0),
        _record(2, Outcome.SUCCESS_AFTER_REPAIR, 20.0, impl=2, debug=1),
        _record(3, Outcome.ENVIRONMENT_INCOMPATIBLE, 5.0),
    ]
    s = summarize(cfg, records)
    assert s.issues_attempted == 3
    assert s.successful == 1
    assert s.successful_after_repair == 1
    assert s.environment_failures == 1
    assert abs(s.success_rate - 1 / 3) < 1e-3
    assert s.average_duration_s == 11.7
    assert s.median_duration_s == 10.0
    assert s.model_usage_by_tier["strong"]["calls"] == 3


def test_markdown_contains_required_sections():
    cfg = BenchmarkConfig(repo="o/r", count=1)
    records = [_record(7, Outcome.SUCCESS, 12.0)]
    infos = [
        CandidateInfo(
            number=7,
            title="Issue 7",
            url="https://github.com/o/r/issues/7",
            assessment=records[0].assessment,
            selected=True,
            selection_reasons=["actionable"],
        )
    ]
    report = build_report(cfg, infos, records)
    md = render_markdown(report)
    assert "## Summary" in md
    assert "### #7" in md
    assert "## Aggregate metrics" in md
    assert "Success rate" in md
    assert "## Candidate pool" in md


def test_write_report_creates_files(tmp_path: Path):
    cfg = BenchmarkConfig(repo="o/r", count=1)
    report = build_report(cfg, [], [_record(1, Outcome.TEST_FAILURE, 3.0)])
    json_path, md_path = write_report(report, tmp_path)
    assert json_path.exists() and json_path.name == "benchmark-report.json"
    assert md_path.exists() and md_path.name == "benchmark-report.md"
    assert "TEST_FAILURE" in md_path.read_text()
