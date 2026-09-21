"""Planner: read-only. Produces a concrete implementation plan from issue + repo signals.

The planner never modifies the repo. It scans the workspace for relevant files
(suffix match on triage likely_files + keyword search) and proposes minimal steps.
"""
from __future__ import annotations

import re
from pathlib import Path

from contributor.models.state import ImplementationPlan, TriageResult


def _search_keywords(workspace: Path, keywords: list[str], limit: int = 12) -> list[str]:
    hits: list[str] = []
    if not workspace.exists():
        return hits
    kws = [k.lower() for k in keywords if len(k) > 3][:8]
    if not kws:
        return hits
    for p in workspace.rglob("*"):
        if len(hits) >= limit:
            break
        if not p.is_file() or p.stat().st_size > 200_000:
            continue
        if ".git/" in str(p):
            continue
        if p.suffix not in (".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".rb", ".php", ".md"):
            continue
        try:
            text = p.read_text(errors="ignore")[:20000].lower()
        except Exception:
            continue
        if any(k in text for k in kws):
            hits.append(str(p.relative_to(workspace)))
    return hits


def build_plan(
    issue_title: str,
    issue_body: str,
    triage: TriageResult,
    workspace: Path | None = None,
) -> ImplementationPlan:
    keywords = re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", f"{issue_title} {issue_body}")[:12]
    inspect: list[str] = list(triage.likely_files[:6])
    if workspace is not None:
        for f in _search_keywords(workspace, keywords):
            if f not in inspect:
                inspect.append(f)
    inspect = inspect[:12]
    expected = list(inspect[:4])
    steps = [
        "Reproduce/understand the issue from the report and relevant code.",
        "Implement the minimal fix in the identified files.",
        "Add or update regression tests covering the reported case.",
        "Run the focused tests, then the broader suite for regressions.",
    ]
    if triage.issue_type == "docs":
        steps = ["Locate the documented behavior.", "Update docs.", "Verify rendering/links if applicable."]
    commands = ["<auto-detected by test runner>"]
    return ImplementationPlan(
        objective=f"Resolve issue: {issue_title[:200]}",
        suspected_root_cause=f"Suspected area ({triage.issue_type}): see {', '.join(inspect[:3]) or 'code inspection needed'}.",
        files_to_inspect=inspect,
        files_expected_to_change=expected,
        steps=steps,
        tests_to_add_or_change=["regression test for the reported case"],
        commands_to_run=commands,
        potential_regressions=["behavioral change in touched module; run full related suite"],
        rollback_notes="Revert the fix commit on the contrib branch; base branch untouched until PR merge.",
    )
