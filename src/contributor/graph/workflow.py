"""LangGraph workflow assembly + job runner (persisted, restartable)."""
from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.graph import END, StateGraph

from contributor.config import Settings
from contributor.graph import routing
from contributor.graph.nodes import WorkflowContext, _patch  # noqa
from contributor.graph.nodes import (
    node_blocked_provider,
    node_ci_check,
    node_create_pr,
    node_debug,
    node_env_incompatible,
    node_environment_discovery,
    node_environment_failure,
    node_escalate,
    node_fetch_issue,
    node_finalize_benchmark,
    node_implement,
    node_plan,
    node_prepare_workspace,
    node_resource_incompatible,
    node_review,
    node_test,
    node_triage,
    node_update_pr,
    node_wait_review,
)
from contributor.graph.state import GraphState
from contributor.models.state import JobState, JobStatus
from contributor.observability import events
from contributor.observability.events import emit


def build_graph(ctx: WorkflowContext):
    g = StateGraph(GraphState)

    def wrap(fn):
        return partial(fn, ctx=ctx)

    g.add_node("fetch", wrap(node_fetch_issue))
    g.add_node("triage", wrap(node_triage))
    g.add_node("env_discovery", wrap(node_environment_discovery))
    g.add_node("plan", wrap(node_plan))
    g.add_node("prepare", wrap(node_prepare_workspace))
    g.add_node("implement", wrap(node_implement))
    g.add_node("test", wrap(node_test))
    g.add_node("review", wrap(node_review))
    g.add_node("debug", wrap(node_debug))
    g.add_node("create_pr", wrap(node_create_pr))
    g.add_node("ci_check", wrap(node_ci_check))
    g.add_node("update_pr", wrap(node_update_pr))
    g.add_node("wait", wrap(node_wait_review))
    g.add_node("escalate", wrap(node_escalate))
    g.add_node("benchmark_finalize", wrap(node_finalize_benchmark))
    g.add_node("resource_incompatible", wrap(node_resource_incompatible))
    g.add_node("env_incompatible", wrap(node_env_incompatible))
    g.add_node("environment_failure", wrap(node_environment_failure))
    g.add_node("blocked_provider", wrap(node_blocked_provider))

    g.set_entry_point("fetch")
    g.add_edge("fetch", "triage")
    g.add_conditional_edges("triage", routing.after_triage,
                            {"env_discovery": "env_discovery", "escalate": "escalate"})
    g.add_conditional_edges(
        "env_discovery",
        lambda s: routing.after_environment(s, ctx.settings),
        {"plan": "plan", "env_unsupported": "env_incompatible"},
    )
    g.add_edge("plan", "prepare")
    g.add_conditional_edges(
        "prepare",
        routing.after_prepare,
        {"implement": "implement", "resource_incompatible": "resource_incompatible",
         "environment_failure": "environment_failure", "escalate": "escalate"},
    )
    g.add_conditional_edges(
        "implement",
        routing.after_implement,
        {"test": "test", "blocked_provider": "blocked_provider"},
    )
    g.add_conditional_edges(
        "test",
        lambda s: routing.after_test(s, ctx.settings),
        {"review": "review", "debug": "debug", "escalate": "escalate",
         "env_unsupported": "env_incompatible", "environment_failure": "environment_failure"},
    )
    g.add_conditional_edges(
        "review",
        lambda s: routing.after_review(s, ctx.settings, ctx.benchmark_mode),
        {"create_pr": "create_pr", "update_pr": "update_pr", "finalize_benchmark": "benchmark_finalize",
         "implement": "implement", "debug": "debug", "escalate": "escalate"},
    )
    g.add_conditional_edges(
        "debug",
        routing.after_implement,
        {"test": "test", "blocked_provider": "blocked_provider"},
    )
    g.add_conditional_edges(
        "create_pr",
        routing.after_create_pr,
        {"ci_check": "ci_check", "__end__": END},
    )
    g.add_conditional_edges(
        "ci_check",
        lambda s: routing.after_ci(s, ctx.settings),
        {"debug": "debug", "wait_for_review": "wait", "escalate": "escalate",
         "environment_failure": "environment_failure"},
    )
    g.add_edge("update_pr", "ci_check")
    g.add_conditional_edges("wait", routing.after_wait, {"implement": "implement", "__end__": END})
    g.add_edge("escalate", END)
    g.add_edge("benchmark_finalize", END)
    g.add_edge("resource_incompatible", END)
    g.add_edge("env_incompatible", END)
    g.add_edge("environment_failure", END)
    g.add_edge("blocked_provider", END)
    return g.compile()


def initial_state_for_job(job: JobState) -> dict[str, Any]:
    return job.model_dump(mode="python")


def run_to_completion(ctx: WorkflowContext, job: JobState, *, max_steps: int = 60) -> JobState:
    """Execute the compiled graph with a step cap (hard limit on agent loops)."""
    app = build_graph(ctx)
    state = initial_state_for_job(job)
    # stream with recursion limit; each node persists incrementally so resume works
    try:
        result = app.invoke(state, config={"recursion_limit": max_steps})
    finally:
        # Dispose the job's isolated environment (idempotent).
        if getattr(ctx, "sessions", None) is not None:
            try:
                ctx.sessions.stop(job.job_id)
            except Exception:
                pass
    final = ctx.repo.get(job.job_id)
    if final is not None:
        return final
    # fallback: rebuild from result dict
    return JobState(**{k: v for k, v in result.items() if k in JobState.model_fields})
