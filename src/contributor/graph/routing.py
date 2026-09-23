"""Routing: conditional edges. All limits enforced here — no infinite loops.

Deterministic inputs only: triage decision, test exit codes (via test_results),
review verdict, CI state, attempt counters vs settings caps.
"""
from __future__ import annotations

from contributor.config import Settings


def after_triage(state: dict) -> str:
    tr = state.get("triage_result") or {}
    d = tr.get("decision", "reject")
    if d == "accept":
        return "env_discovery"
    return "escalate"


def _strategy(state: dict) -> tuple[str, bool]:
    report = state.get("environment_report") or {}
    strategy = report.get("strategy") or "standard_docker"
    if isinstance(strategy, dict):  # enum serialized defensively
        strategy = strategy.get("value", "standard_docker")
    compatible = report.get("container_compatible", True)
    return str(strategy), bool(compatible)


def after_environment(state: dict, settings: Settings) -> str:
    """Hard-gate only truly incompatible repos; otherwise proceed (env failures
    are still classified without entering the debug loop)."""
    strategy, compatible = _strategy(state)
    if strategy == "incompatible":
        return "env_unsupported"
    if strategy == "host_required" and settings.env_hard_gate:
        return "env_unsupported"
    return "plan"


def after_prepare(state: dict) -> str:
    """Provisioning may terminate with an environment/resource state."""
    cs = state.get("current_state", "")
    if cs == "resource_incompatible":
        return "resource_incompatible"
    if cs == "environment_failure":
        return "environment_failure"
    if cs == "escalate":
        return "escalate"
    return "implement"


def after_implement(state: dict) -> str:
    """Provider failures cannot be fixed by testing/debugging — stop immediately."""
    meta = state.get("issue_metadata") or {}
    if meta.get("provider_blocked"):
        return "blocked_provider"
    return "test"


def after_test(state: dict, settings: Settings) -> str:
    results = state.get("test_results") or []
    if not results:
        return "debug_or_escalate"
    last = results[-1]
    # No runnable test command for this ecosystem: the repo cannot be tested in
    # this sandbox. Terminal (not a test failure — debugging cannot fix it).
    if last.get("command") == "(no test command)" and not last.get("passed"):
        return "env_unsupported"
    # Failures dominated by missing host tooling cannot be fixed by editing code.
    # CASE B (repo cannot run here) vs CASE E (deps/environment missing).
    if not last.get("passed") and last.get("environment_related"):
        strategy, compatible = _strategy(state)
        if strategy in ("host_required", "incompatible") or not compatible:
            return "env_unsupported"
        return "environment_failure"
    if last.get("passed"):
        return "review"
    # failed: bounded debug
    if (state.get("debug_attempt", 0) + state.get("implementation_attempt", 0)) < (
        settings.max_debug_attempts + settings.max_implementation_attempts
    ) and state.get("debug_attempt", 0) < settings.max_debug_attempts:
        return "debug"
    return "escalate"


def after_review(state: dict, settings: Settings, benchmark: bool = False) -> str:
    rr = state.get("review_result") or {}
    v = rr.get("verdict", "human_required")
    has_pr = bool(state.get("pull_request_url"))
    if v == "approved":
        if benchmark:
            return "finalize_benchmark"
        return "update_pr" if has_pr else "create_pr"
    if v == "human_required":
        return "escalate"
    # changes_required -> bounded re-implement
    cycles = state.get("review_attempt", 0)
    impl = state.get("implementation_attempt", 0)
    if cycles >= settings.max_review_cycles or impl >= settings.max_implementation_attempts:
        return "escalate"
    # if tests failed, debug is more appropriate than blind re-implement
    results = state.get("test_results") or []
    if results and not results[-1].get("passed"):
        if state.get("debug_attempt", 0) < settings.max_debug_attempts:
            return "debug"
        return "escalate"
    return "implement"


def after_create_pr(state: dict) -> str:
    """After publishing: terminal for push-only mode, else continue to CI."""
    return "__end__" if state.get("done") else "ci_check"


def after_ci(state: dict, settings: Settings | None = None) -> str:
    ci = state.get("ci_result") or {}
    s = ci.get("state", "unknown")
    if s != "fail":
        return "wait_for_review"
    cls = state.get("ci_failure_class") or "unknown"
    if isinstance(cls, dict):
        cls = cls.get("value", "unknown")
    if cls == "environment_failure":
        return "environment_failure"
    if cls == "ci_infrastructure_failure":
        return "escalate"
    max_cycles = settings.max_ci_repair_cycles if settings else 2
    if state.get("ci_repair_attempt", 0) > max_cycles:
        return "escalate"
    return "debug"


def after_wait(state: dict) -> str:
    if state.get("done"):
        return "__end__"
    cur = state.get("current_state", "")
    if cur == "implement":
        return "implement"
    return "__end__"


def after_debug_check(state: dict, settings: Settings) -> str:
    if state.get("debug_attempt", 0) > settings.max_debug_attempts:
        return "escalate"
    return "test"
