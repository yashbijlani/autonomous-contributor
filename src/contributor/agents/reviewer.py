"""Independent reviewer: evaluates the diff, not the implementer's claims.

Deterministic checks (always run):
- empty diff -> changes_required
- unrelated-file heuristics, secret patterns, huge diffs
- test results must exist and pass (LLM never overrides exit codes)
Heuristic content review adds issues; verdict is computed, not asked from an LLM
by default (keeps tests deterministic). An LLM pass can be layered later.
"""
from __future__ import annotations

import re

from contributor.models.state import ReviewIssue, ReviewResult, ReviewVerdict, TestResult

SECRET_IN_DIFF = [
    r"AKIA[0-9A-Z]{16}", r"ghp_[A-Za-z0-9]{20,}", r"-----BEGIN (RSA )?PRIVATE KEY-----",
    r"aws_secret_access_key", r"password\s*=\s*['\"][^'\"]+['\"]",
]
RISKY_IN_DIFF = [
    r"rm\s+-rf", r"chmod\s+777", r"curl.*\|\s*sh", r"eval\(", r"exec\(",
]

MAX_REASONABLE_DIFF_LINES = 2000


def review_change(
    *,
    diff: str,
    changed_files: list[str],
    test_results: list[TestResult],
    issue_title: str = "",
    plan_objective: str = "",
) -> ReviewResult:
    issues: list[ReviewIssue] = []
    # 1. empty diff
    if not diff.strip():
        return ReviewResult(
            verdict=ReviewVerdict.CHANGES_REQUIRED,
            issues=[ReviewIssue(severity="blocker", message="Empty diff: no changes were made.")],
            summary="No changes detected; implementation produced no diff.",
            confidence=0.95,
        )
    lines = diff.splitlines()
    # 2. huge diff
    if len(lines) > MAX_REASONABLE_DIFF_LINES:
        issues.append(ReviewIssue(severity="major", message=f"Diff very large ({len(lines)} lines); fix should be minimal."))
    # 3. secrets
    for pat in SECRET_IN_DIFF:
        if re.search(pat, diff):
            issues.append(ReviewIssue(severity="blocker", message=f"Possible secret in diff (pattern {pat!r})."))
    # 4. risky ops
    for pat in RISKY_IN_DIFF:
        if re.search(pat, diff):
            issues.append(ReviewIssue(severity="major", message=f"Risky construct in diff (pattern {pat!r})."))
    # 5. lockfile-only or unrelated churn
    if changed_files and all(f.endswith((".lock", "-lock.json", ".min.js")) for f in changed_files):
        issues.append(ReviewIssue(severity="major", message="Only lockfile/generated files changed; no source fix."))
    if len(changed_files) > 15:
        issues.append(ReviewIssue(severity="minor", message=f"Many files changed ({len(changed_files)}); check for unrelated churn."))
    # 6. tests: must exist + pass (deterministic)
    if not test_results:
        issues.append(ReviewIssue(severity="major", message="No test results recorded; cannot verify the fix."))
    else:
        last = test_results[-1]
        if not last.passed:
            issues.append(ReviewIssue(severity="blocker", message=f"Tests failing: {last.command} exit={last.exit_code}."))
        # fix without touching tests at all is suspicious (unless docs)
        test_touched = any(re.search(r"test|spec", f, re.I) for f in changed_files)
        if not test_touched and last.passed:
            issues.append(ReviewIssue(severity="minor", message="No test files changed; consider adding a regression test."))
    blockers = [i for i in issues if i.severity == "blocker"]
    majors = [i for i in issues if i.severity == "major"]
    if blockers:
        verdict = ReviewVerdict.CHANGES_REQUIRED
        summary = "Blocked: " + "; ".join(i.message for i in blockers)
    elif len(majors) >= 2:
        verdict = ReviewVerdict.CHANGES_REQUIRED
        summary = "Changes required: " + "; ".join(i.message for i in majors)
    elif majors:
        verdict = ReviewVerdict.CHANGES_REQUIRED
        summary = majors[0].message
    else:
        verdict = ReviewVerdict.APPROVED
        summary = "Change looks minimal, tests pass, no secrets/risky patterns found." + (
            f" Notes: {'; '.join(i.message for i in issues)}" if issues else ""
        )
    conf = 0.9 if verdict == ReviewVerdict.APPROVED and not issues else 0.75
    return ReviewResult(verdict=verdict, issues=issues, summary=summary, confidence=conf)
