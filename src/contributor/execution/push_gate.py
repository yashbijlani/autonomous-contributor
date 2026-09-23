"""Deterministic final gate before any GitHub push.

Program-decided, never LLM-decided. If any required condition is false the
push is refused and the job ends in PUSH_GATE_REJECTED (distinct from a code,
environment or push failure).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contributor.config import Settings
from contributor.models.state import JobState, JobStatus

PROTECTED_BRANCHES = {"main", "master"}

_BANNED_PATH = re.compile(
    r"(^|/)(\.env(\..*)?|id_rsa|id_ed25519|\.git-credentials|\.npmrc|"
    r"credentials\.json|.*\.pem|.*\.key)$",
    re.IGNORECASE,
)
_TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass
class PushCheck:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class PushGateResult:
    allowed: bool
    checks: list[PushCheck] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
            "reasons": self.reasons,
        }


def _add(checks: list[PushCheck], reasons: list[str], name: str, ok: bool, detail: str = "") -> None:
    checks.append(PushCheck(name, bool(ok), detail))
    if not ok:
        reasons.append(f"{name}: {detail}" if detail else name)


def run_push_gate(
    *,
    job: JobState,
    workspace: Path | None,
    settings: Settings,
    allow_push: bool,
    execution_mode: str,
    target_repo: str,
    remote: str,
    expected_repo: str,
) -> PushGateResult:
    checks: list[PushCheck] = []
    reasons: list[str] = []

    _add(checks, reasons, "mode_permits_push",
         allow_push and execution_mode == "live",
         f"mode={execution_mode} allow_push={allow_push}")

    tr = job.triage_result
    _add(checks, reasons, "triage_accepted",
         bool(tr and tr.decision.value == "accept"),
         tr.decision.value if tr else "no triage")

    _add(checks, reasons, "repository_matches",
         bool(expected_repo) and job.repository == expected_repo,
         f"{job.repository} != expected {expected_repo}")

    branch = job.branch_name or ""
    default = ""
    if isinstance(job.issue_metadata, dict):
        default = str(job.issue_metadata.get("default_branch", "") or "")
    protected = branch.lower() in PROTECTED_BRANCHES or (
        bool(default) and branch.lower() == default.lower()
    )
    _add(checks, reasons, "branch_not_default", bool(branch) and not protected, f"branch={branch}")

    ws: Path | None = None
    ws_ok = False
    if workspace is not None:
        try:
            ws = Path(workspace).resolve()
            root = Path(settings.workspaces_root).expanduser().resolve()
            ws_ok = ws.exists() and (
                ws == root or root in ws.parents
            ) and ws.name.startswith("job-")
        except Exception:
            ws_ok = False
    _add(checks, reasons, "workspace_expected", ws_ok, str(workspace))

    bad_states = {
        JobStatus.ENVIRONMENT_FAILURE,
        JobStatus.REPOSITORY_ENVIRONMENT_INCOMPATIBLE,
        JobStatus.RESOURCE_INCOMPATIBLE,
        JobStatus.BLOCKED_PROVIDER,
    }
    _add(checks, reasons, "not_environment_or_resource_failure",
         job.current_state not in bad_states, job.current_state.value)

    _add(checks, reasons, "implementation_completed",
         job.implementation_attempt >= 1, f"attempts={job.implementation_attempt}")

    files: list[str] = []
    if ws_ok and ws is not None:
        from contributor.execution.git import working_tree_files

        files = [f for f in working_tree_files(ws) if not f.startswith(".contributor")]
        if not files and job.commit_sha:
            # Resume path: the contribution is already committed on this branch.
            # Gate the committed change relative to its parent, not the clean tree.
            from contributor.execution.commands import run_command

            r = run_command(
                ["git", "-C", str(ws), "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
                timeout=30,
            )
            if r.ok:
                files = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    _add(checks, reasons, "source_changes_exist", bool(files), f"{len(files)} file(s)")

    banned = [f for f in files if _BANNED_PATH.search(f)]
    _add(checks, reasons, "no_secret_or_unexpected_files", not banned, ",".join(banned[:5]))

    only_lock = bool(files) and all(
        f.endswith((".lock", "-lock.json", ".lockb")) or f.endswith("lock") for f in files
    )
    _add(checks, reasons, "not_lockfile_only", not only_lock, ",".join(files[:5]))

    last = job.test_results[-1] if job.test_results else None
    _add(checks, reasons, "targeted_tests_passed",
         bool(last and last.passed),
         f"cmd={getattr(last, 'command', '')} passed={getattr(last, 'passed', None)}")

    health = job.environment_health if isinstance(job.environment_health, dict) else None
    healthy = bool(health and health.get("healthy")) if health is not None else True
    _add(checks, reasons, "environment_healthy", healthy,
         str(health.get("missing_tools") if health else "no health record"))

    rr = job.review_result
    approved = bool(rr and rr.verdict.value == "approved")
    blockers = [i for i in (rr.issues if rr else []) if i.severity == "blocker"]
    _add(checks, reasons, "review_approved", approved,
         (rr.summary[:120] if rr else "no review"))
    _add(checks, reasons, "no_blocking_review_findings", not blockers, f"{len(blockers)} blocker(s)")

    _add(checks, reasons, "push_target_valid", bool(target_repo and _TARGET_RE.match(target_repo)),
         target_repo or "(empty)")
    _add(checks, reasons, "remote_configured", bool(remote), remote or "(empty)")

    return PushGateResult(allowed=not reasons, checks=checks, reasons=reasons)
