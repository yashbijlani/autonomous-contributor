"""Benchmark mode: non-destructive, measurable evaluation of the contributor
against real GitHub repositories.

Reuses the existing discovery/triage/environment/workflow pipeline. Adds only:
- a structured solvability assessment (explicit factual dimensions),
- transparent selection filters,
- a no-push/no-PR terminal path through the graph,
- per-issue metrics + JSON/Markdown reports.
"""
from contributor.benchmark.models import (
    BenchmarkConfig,
    BenchmarkRecord,
    BenchmarkReport,
    BenchmarkSummary,
    CandidateInfo,
    Outcome,
)

__all__ = [
    "BenchmarkConfig",
    "BenchmarkRecord",
    "BenchmarkReport",
    "BenchmarkSummary",
    "CandidateInfo",
    "Outcome",
]
