"""Prompt builders for the OpenCode implementation worker.

The orchestrator owns git/PR lifecycle; prompts explicitly forbid OpenCode from
committing or pushing.
"""
from __future__ import annotations

from contributor.models.state import JobState

BASE_RULES = """You are an expert software engineer working inside a repository checkout.
Rules:
- Inspect existing implementation before changing it.
- Make the smallest reasonable change that fixes the issue.
- Preserve existing public APIs unless the issue requires otherwise.
- Add or update regression tests covering the fix.
- Follow repository conventions (style, lint, CONTRIBUTING.md).
- Run only the SMALLEST meaningful check for what you changed (e.g. a single
  crate/module test or `cargo check -p <crate>`). Do NOT run the full repository
  suite or full release builds — the orchestrator runs the broader tests.
- Do NOT modify unrelated files.
- Do NOT commit, do NOT push, do NOT create branches — the orchestrator owns git.
- Do NOT access network services with credentials.
- Stay inside the repository. Do ALL scratch/repro work in the
  `.contributor-scratch/` directory at the repository root (it is git-ignored).
  NEVER write to /tmp, $HOME, or any path outside the repository — such access is
  auto-rejected in this non-interactive environment and wastes the attempt.
- Prefer editing files directly; do not rely on interactive tools.
- At the end, report: files changed, tests run, and any limitations.
"""


def build_implement_prompt(state: JobState, *, extra_context: str = "") -> str:
    triage = state.triage_result.model_dump_json(indent=2) if state.triage_result else "{}"
    plan = state.plan.model_dump_json(indent=2) if state.plan else "{}"
    failures = ""
    if state.test_results:
        last = state.test_results[-1]
        failures = (
            f"\nPrevious test failure evidence (must address):\n"
            f"command: {last.command}\nexit: {last.exit_code}\n"
            f"stdout tail:\n{last.stdout[-3000:]}\nstderr tail:\n{last.stderr[-3000:]}"
        )
    review = ""
    if state.review_result and state.review_result.verdict.value != "approved":
        items = "\n".join(f"- [{i.severity}] {i.file}: {i.message}" for i in state.review_result.issues)
        review = f"\nReviewer feedback (must address):\n{state.review_result.summary}\n{items}"
    prior = ""
    if state.errors:
        prior = "\nPrevious attempt errors (address these; do not repeat):\n" + "\n".join(
            f"- {e[:300]}" for e in state.errors[-3:]
        )
    return f"""{BASE_RULES}
GitHub issue #{state.issue_number}: {state.issue_title}
Body:
{state.issue_body[:6000]}

Triage:
{triage}

Plan:
{plan}
{failures}
{review}
{prior}
{extra_context}
Implement the fix now. Keep the diff minimal and run the relevant tests if quick.
"""


def build_debug_prompt(state: JobState, *, failing: str = "") -> str:
    return build_implement_prompt(state, extra_context=f"\nDEBUG MODE. Failing evidence:\n{failing[:5000]}\nFix the root cause, not the symptom.")


def _ci_repair_context(state: JobState) -> str:
    diag = state.ci_diagnosis or {}
    failed = diag.get("failed_checks") or []
    error_lines = diag.get("error_lines") or []
    jobs = diag.get("jobs") or []
    parts = [
        "CI REPAIR ITERATION.",
        f"Issue: #{state.issue_number} {state.issue_title}",
        f"PR: {state.pull_request_url or '(none)'}",
        f"Current commit: {state.commit_sha or '(unknown)'}",
        f"Changed files: {', '.join(c.get('file', '') for c in (diag.get('changed_files') or [])) or '(see diff)'}",
        f"Failure classification: {state.ci_failure_class.value if state.ci_failure_class else 'unknown'}",
    ]
    if failed:
        parts.append("Failed checks:")
        for c in failed[:5]:
            parts.append(
                f"- {c.get('name')} [{c.get('workflow') or '-'}] "
                f"conclusion={c.get('conclusion')} url={c.get('url')}"
            )
    if jobs:
        parts.append("Failed jobs: " + ", ".join(str(j.get('check')) for j in jobs[:5]))
    if state.ci_repair_attempt:
        parts.append(f"Previous repair attempts: {state.ci_repair_attempt}")
    if state.test_results:
        last = state.test_results[-1]
        parts.append(
            f"Last local test: `{last.command}` exit={last.exit_code} passed={last.passed}"
        )
    repro = state.issue_metadata.get("ci_reproduction") if isinstance(state.issue_metadata, dict) else None
    if repro:
        parts.append(f"Local reproduction: {repro}")
    if error_lines:
        parts.append("Relevant CI log lines:")
        parts.append("\n".join(error_lines[:40]))
    return "\n".join(parts)


def build_ci_repair_prompt(state: JobState, *, failing: str = "") -> str:
    context = _ci_repair_context(state)
    return f"""{BASE_RULES}
{context}

This is a CI repair iteration.
Do not rewrite the implementation unnecessarily.
Diagnose the failure first.
Make the smallest correct change.
Reproduce the failure locally when possible.
Run the narrowest relevant test before broader validation.
If the CI failure is infrastructure/environment/flaky rather than caused by
this change, make NO code change and report that finding instead.
{failing}
"""
