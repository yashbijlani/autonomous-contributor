"""CI interpretation, polling and failure classification.

Deterministic by design: GitHub check states and conclusions decide the verdict.
The LLM never decides whether CI passed, and never classifies a failure.

Normalized check representation (persisted in ``CIResult.checks``):
    {name, workflow, status, conclusion, url, started_at, completed_at,
     duration_s, app, required, state}
``state`` is the backward-compatible per-check view: pass|fail|pending.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable

from contributor.github.client import GitHubClient
from contributor.models.state import CIFailureClass, CIResult, EnvironmentReport

_PASS_CONCLUSIONS = {"success", "neutral", "skipped"}
_FAIL_CONCLUSIONS = {"failure", "timed_out", "action_required", "cancelled", "canceled", "stale", "startup_failure"}

# Log/annotation signals. Order matters: the first match wins.
_RESOURCE_SIGNALS = (
    "out of memory", "oom", "killed process", "ran out of memory", "exit code 137",
    "no space left on device", "disk quota", "resource exhausted",
)
_PERMISSION_SIGNALS = (
    "permission denied", "access denied", "forbidden", "403", "not authorized",
    "resource not accessible", "bad credentials", "secret", "must be logged in",
)
_NETWORK_SIGNALS = (
    "econnrefused", "econnreset", "etimedout", "enotfound", "getaddrinfo",
    "could not resolve host", "connection timed out", "network is unreachable",
    "tls handshake", "socket hang up", "429", "rate limit", "502", "503", "504",
    "service unavailable", "bad gateway", "gateway timeout",
)
_DEPENDENCY_SIGNALS = (
    "dependency", "dependencies", "could not find package", "no matching distribution",
    "package not found", "cannot find module", "module not found", "npm err",
    "pip install", "apt-get", "unable to locate package", "failed to resolve",
    "version solving failed", "lock file", "registry",
)
_ENV_SIGNALS = (
    "missing", "not found", "no such file", "setup-python", "setup-node",
    "setup-java", "setup-go", "toolchain", "environment variable",
)
_INFRA_SIGNALS = (
    "cancelled", "canceled", "timed_out", "timeout", "action_required",
    "stale", "runner", "startup", "lost communication", "no runner",
    "infrastructure", "the job was not acquired", "error: the operation was canceled",
)
_BUILD_SIGNALS = (
    "compile", "compilation", "build failed", "cargo", "rustc", "tsc",
    "gcc", "clang", "gradle", "maven", "make", "linker", "syntax error",
)
_TEST_SIGNALS = (
    "assert", "assertionerror", "test failed", "pytest", "jest", "vitest",
    "cargo test", "go test", "rspec", "failed test", "failures:",
)
_FLAKY_SIGNALS = (
    "flaky", "intermittent", "retry", "rerun", "timed out waiting",
    "randomly", "nondeterministic",
)
_CODE_SIGNALS = ("test", "build", "compile", "lint", "unit", "integration", "ci", "check", "type")

_ERROR_LINE = re.compile(
    r"(?i)^(?:error|err!|fatal|panic|failed|failure|assertionerror|"
    r"not ok|×|✕|FAILED|##\[error\]|E\s|npm ERR!|error\[E\d+\])"
)


def _check_state(status: str, conclusion: str) -> str:
    status = (status or "").lower()
    conclusion = (conclusion or "").lower()
    if status and status != "completed":
        return "pending"
    if conclusion in _PASS_CONCLUSIONS:
        return "pass"
    if conclusion in _FAIL_CONCLUSIONS or conclusion:
        return "fail"
    return "pending"


def _duration(started: str, completed: str) -> float:
    from datetime import datetime

    def _parse(v: str) -> datetime | None:
        if not v:
            return None
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None

    a, b = _parse(started or ""), _parse(completed or "")
    if a and b:
        return max(0.0, (b - a).total_seconds())
    return 0.0


def normalize_check_run(cr: dict[str, Any]) -> dict[str, Any]:
    status = str(cr.get("status", "") or "")
    conclusion = str(cr.get("conclusion", "") or "")
    suite = cr.get("check_suite") or {}
    app = (cr.get("app") or {}).get("name") or (suite.get("app") or {}).get("name") or ""
    workflow = ""
    if isinstance(suite, dict):
        wr = suite.get("workflow_run") or {}
        if isinstance(wr, dict):
            workflow = wr.get("name") or ""
    started = cr.get("started_at") or ""
    completed = cr.get("completed_at") or ""
    return {
        "name": cr.get("name") or "(unnamed check)",
        "workflow": workflow,
        "status": status.lower(),
        "conclusion": conclusion.lower(),
        "url": cr.get("html_url") or cr.get("details_url") or "",
        "details_url": cr.get("details_url") or "",
        "started_at": started,
        "completed_at": completed,
        "duration_s": _duration(started, completed),
        "app": app,
        "id": cr.get("id"),
        "required": False,
        "state": _check_state(status, conclusion),
    }


def normalize_status(s: dict[str, Any]) -> dict[str, Any]:
    state = str(s.get("state", "") or "").lower()
    mapped = {"success": "pass", "pending": "pending"}.get(state, "fail" if state else "pending")
    return {
        "name": s.get("context") or "(status)",
        "workflow": "",
        "status": "completed",
        "conclusion": state,
        "url": s.get("target_url") or "",
        "details_url": s.get("target_url") or "",
        "started_at": "",
        "completed_at": s.get("updated_at") or "",
        "duration_s": 0.0,
        "app": "",
        "id": None,
        "required": False,
        "state": mapped,
    }


def normalize_checks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    combined = payload.get("combined") or {}
    check_runs = payload.get("check_runs") or []
    checks: list[dict[str, Any]] = [normalize_status(s) for s in (combined.get("statuses") or [])]
    checks.extend(normalize_check_run(cr) for cr in check_runs)
    return checks


def _aggregate(checks: list[dict[str, Any]], combined_state: str = "") -> str:
    if not checks:
        if combined_state == "success":
            return "pass"
        if combined_state in ("failure", "error"):
            return "fail"
        if combined_state == "pending":
            return "pending"
        return "unknown"
    states = [c["state"] for c in checks]
    if "fail" in states:
        return "fail"
    if "pending" in states:
        return "pending"
    return "pass"


def _phase(checks: list[dict[str, Any]]) -> str:
    if not checks:
        return ""
    statuses = {str(c.get("status", "")) for c in checks}
    if "in_progress" in statuses:
        return "in_progress"
    if statuses & {"queued", "requested", "waiting", "pending"}:
        return "queued"
    if statuses == {"completed"}:
        return "completed"
    return ""


def interpret_ci(payload: dict[str, Any]) -> CIResult:
    combined = payload.get("combined") or {}
    checks = normalize_checks(payload)
    combined_state = str(combined.get("state", "") or "").lower()
    state = _aggregate(checks, combined_state)
    phase = _phase(checks)
    summary = f"{len(checks)} checks: {state}" + (f" ({phase})" if phase else "")
    return CIResult(
        state=state,  # type: ignore[arg-type]
        checks=checks,
        summary=summary,
        phase=phase,
        total=len(checks),
    )


def fetch_ci(client: GitHubClient, owner: str, repo: str, sha: str) -> CIResult:
    payload = client.get_ci_status(owner, repo, sha)
    return interpret_ci(payload)


def apply_required(ci: CIResult, required_names: list[str]) -> CIResult:
    """Mark checks as required (best-effort) and recompute required counts."""
    required = {n.strip().lower() for n in required_names if n}
    for c in ci.checks:
        c["required"] = str(c.get("name", "")).strip().lower() in required
    ci.required_total = sum(1 for c in ci.checks if c.get("required"))
    ci.required_pending = sum(1 for c in ci.checks if c.get("required") and c.get("state") == "pending")
    ci.required_failed = sum(1 for c in ci.checks if c.get("required") and c.get("state") == "fail")
    return ci


def watch_ci(
    client: GitHubClient,
    owner: str,
    repo: str,
    sha: str,
    *,
    interval_s: float = 20.0,
    max_wait_s: float = 1800.0,
    settle_s: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    on_update: Callable[[CIResult], None] | None = None,
) -> CIResult:
    """Poll checks until terminal or bounded limits are reached.

    Returns the latest CIResult. ``timed_out`` is True when the wait limit was
    hit while checks were still pending, or while none had appeared yet.
    """
    start = now()
    polls = 0
    last = CIResult()
    while True:
        payload = client.get_ci_status(owner, repo, sha)
        last = interpret_ci(payload)
        polls += 1
        last.polls = polls
        last.waited_s = round(now() - start, 1)
        if on_update:
            on_update(last)
        if last.state in ("pass", "fail"):
            return last
        elapsed = now() - start
        if not last.checks:
            # GitHub reports a combined state of "pending" for a commit with no
            # checks at all. Use the settle window to distinguish "checks not
            # registered yet" from "this repository exposes no checks"; the
            # latter must not block until the maximum wait.
            if elapsed < settle_s and elapsed < max_wait_s:
                sleep(interval_s)
                continue
            last.state = "unknown"
            return last
        if last.state == "pending":
            if elapsed >= max_wait_s:
                last.timed_out = True
                return last
            sleep(interval_s)
            continue
        return last


def _log_blob(ci: CIResult, logs: str) -> str:
    parts: list[str] = []
    for c in ci.checks:
        if c.get("state") != "fail":
            continue
        parts.append(str(c.get("name", "")))
        parts.append(str(c.get("conclusion", "")))
        parts.append(str(c.get("workflow", "")))
    parts.append(logs or "")
    return "\n".join(parts).lower()


def classify_ci_failure(
    ci: CIResult,
    report: EnvironmentReport | None = None,
    *,
    logs: str = "",
    flaky: bool = False,
) -> CIFailureClass:
    """Deterministic classification (never LLM-decided).

    Ordering: resource/permission/network (infrastructure) -> CI infra ->
    flaky -> environment/dependency -> build -> test/code -> unknown.
    """
    if flaky:
        return CIFailureClass.FLAKY_FAILURE
    blob = _log_blob(ci, logs)
    names = " ".join(str(c.get("name", "")) for c in ci.checks if c.get("state") == "fail").lower()

    if any(s in blob for s in _RESOURCE_SIGNALS):
        return CIFailureClass.RESOURCE_FAILURE
    if any(s in blob for s in _PERMISSION_SIGNALS):
        return CIFailureClass.PERMISSION_FAILURE
    if any(s in blob for s in _NETWORK_SIGNALS):
        return CIFailureClass.NETWORK_FAILURE
    if any(s in blob for s in _INFRA_SIGNALS):
        return CIFailureClass.CI_INFRASTRUCTURE_FAILURE
    if any(s in blob for s in _FLAKY_SIGNALS):
        return CIFailureClass.FLAKY_FAILURE
    if report is not None and not report.container_compatible:
        return CIFailureClass.ENVIRONMENT_FAILURE
    if any(s in blob for s in _DEPENDENCY_SIGNALS):
        return CIFailureClass.DEPENDENCY_FAILURE
    if any(s in blob for s in _ENV_SIGNALS):
        return CIFailureClass.ENVIRONMENT_FAILURE
    if any(s in blob for s in _BUILD_SIGNALS):
        return CIFailureClass.BUILD_FAILURE
    if any(s in blob for s in _TEST_SIGNALS):
        return CIFailureClass.TEST_FAILURE
    if any(s in names for s in _CODE_SIGNALS) or any(s in blob for s in _CODE_SIGNALS):
        return CIFailureClass.CODE_FAILURE
    return CIFailureClass.UNKNOWN


def extract_error_lines(text: str, *, limit: int = 40, max_chars: int = 12000) -> list[str]:
    """Keep only likely-error lines (bounded), preserving order."""
    out: list[str] = []
    if not text:
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if _ERROR_LINE.search(s) or "Traceback (most recent call last)" in s:
            out.append(s[:400])
        if len(out) >= limit:
            break
    return out[:limit]


def actions_run_id_from_url(url: str) -> str:
    m = re.search(r"/actions/runs/(\d+)", url or "")
    return m.group(1) if m else ""


def build_diagnosis(
    client: GitHubClient,
    owner: str,
    repo: str,
    ci: CIResult,
    *,
    budget_chars: int = 12000,
) -> tuple[dict[str, Any], str]:
    """Compact diagnosis + a raw artifact reference, from failed checks.

    Fetches check-run output and (best-effort) Actions job logs for failed
    checks, extracts error lines only, and never returns an unbounded blob.
    """
    failed = [c for c in ci.checks if c.get("state") == "fail"]
    diagnosis: dict[str, Any] = {
        "failed_checks": [
            {
                "name": c.get("name"),
                "workflow": c.get("workflow"),
                "conclusion": c.get("conclusion"),
                "url": c.get("url"),
            }
            for c in failed
        ],
        "error_lines": [],
        "jobs": [],
        "raw_refs": [],
        "log_excerpt": "",
    }
    remaining = max(0, budget_chars)
    excerpts: list[str] = []
    for c in failed:
        cid = c.get("id")
        if not cid:
            continue
        run = client.get_check_run(owner, repo, cid) if hasattr(client, "get_check_run") else {}
        output = (run or {}).get("output") or {}
        blob = "\n".join(str(output.get(k, "")) for k in ("title", "summary", "text")).strip()
        job_id = run.get("id")
        details = str(run.get("details_url") or c.get("details_url") or "")
        run_id = actions_run_id_from_url(details)
        if run_id and job_id and hasattr(client, "get_actions_job_logs"):
            logs = ""
            try:
                logs = client.get_actions_job_logs(owner, repo, job_id) or ""
            except Exception:
                logs = ""
            if logs:
                blob = (blob + "\n" + logs).strip()
                diagnosis["raw_refs"].append(details)
        if not blob:
            continue
        excerpts.append(blob)
        lines = extract_error_lines(blob, max_chars=remaining)
        for line in lines:
            if line not in diagnosis["error_lines"]:
                diagnosis["error_lines"].append(line)
                remaining -= len(line)
                if remaining <= 0:
                    break
        diagnosis["jobs"].append({"check": c.get("name"), "details_url": details})
        if remaining <= 0:
            break
    diagnosis["log_excerpt"] = "\n".join(excerpts)[:budget_chars]
    raw_ref = diagnosis["raw_refs"][0] if diagnosis["raw_refs"] else (failed[0].get("url") if failed else "")
    return diagnosis, raw_ref


def classify_with_diagnosis(
    client: GitHubClient,
    owner: str,
    repo: str,
    ci: CIResult,
    report: EnvironmentReport | None = None,
) -> tuple[CIFailureClass, dict[str, Any], str]:
    """Fetch compact logs, classify deterministically, return (class, diag, raw)."""
    diag, raw = build_diagnosis(client, owner, repo, ci)
    logs = diag.get("log_excerpt") or "\n".join(diag.get("error_lines") or [])
    cls = classify_ci_failure(ci, report, logs=logs)
    return cls, diag, raw
