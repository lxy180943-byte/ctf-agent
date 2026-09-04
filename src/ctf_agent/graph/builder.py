"""Compile the checkpointed LangGraph CTF workflow without replacing the live solver."""

from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from ctf_agent.core.config import DEFAULT_GRAPH_BUDGETS, resolve_graph_budgets
from ctf_agent.core.models import utc_now
from ctf_agent.graph.edges import after_human_review, after_select_experiment, after_verify, budget_diagnostic
from ctf_agent.graph.nodes import (
    collect_initial_evidence, execute_experiment, fail_run, finish_run,
    human_review, ingest_challenge, reason_about_challenge, retrieve_memory,
    retrieve_skills, select_experiment, summarize_observation,
    update_hypotheses, verify_candidates,
)
from ctf_agent.graph.state import WorkflowState


def build_workflow(
    *,
    checkpointer: Any | None = None,
    command_timeout_seconds: int = DEFAULT_GRAPH_BUDGETS.command_timeout_seconds,
    max_tool_calls: int | None = DEFAULT_GRAPH_BUDGETS.max_tool_calls,
    max_network_requests: int = DEFAULT_GRAPH_BUDGETS.max_network_requests,
    run_timeout_seconds: int = DEFAULT_GRAPH_BUDGETS.run_timeout_seconds,
    max_repeated_actions: int = DEFAULT_GRAPH_BUDGETS.max_repeated_actions,
    max_consecutive_failures: int = DEFAULT_GRAPH_BUDGETS.max_consecutive_failures,
):
    """Compile a resumable, bounded workflow; pass a durable saver in production."""
    budgets = resolve_graph_budgets(
        {},
        overrides={
            "command_timeout_seconds": command_timeout_seconds,
            "max_tool_calls": max_tool_calls,
            "max_network_requests": max_network_requests,
            "run_timeout_seconds": run_timeout_seconds,
            "max_repeated_actions": max_repeated_actions,
            "max_consecutive_failures": max_consecutive_failures,
        },
    )
    budget_kwargs = {
        "max_tool_calls": budgets.max_tool_calls,
        "max_network_requests": budgets.max_network_requests,
        "run_timeout_seconds": budgets.run_timeout_seconds,
        "max_repeated_actions": budgets.max_repeated_actions,
        "max_consecutive_failures": budgets.max_consecutive_failures,
    }
    workflow = StateGraph(WorkflowState)
    for name, node in {
        "ingest_challenge": ingest_challenge,
        "collect_initial_evidence": collect_initial_evidence,
        "retrieve_skills": retrieve_skills,
        "retrieve_memory": retrieve_memory,
        "reason_about_challenge": reason_about_challenge,
        "select_experiment": select_experiment,
        "execute_experiment": execute_experiment,
        "summarize_observation": summarize_observation,
        "update_hypotheses": update_hypotheses,
        "verify_candidates": verify_candidates,
        "human_review": human_review,
        "finish_run": finish_run,
        "fail_run": partial(_budgeted_fail_run, **budget_kwargs),
    }.items():
        workflow.add_node(name, node)
    workflow.add_edge(START, "ingest_challenge")
    workflow.add_edge("ingest_challenge", "collect_initial_evidence")
    workflow.add_edge("collect_initial_evidence", "retrieve_skills")
    workflow.add_edge("retrieve_skills", "retrieve_memory")
    workflow.add_edge("retrieve_memory", "reason_about_challenge")
    workflow.add_edge("reason_about_challenge", "select_experiment")
    workflow.add_conditional_edges("select_experiment", after_select_experiment, {
        "execute_experiment": "execute_experiment",
        "reason_about_challenge": "reason_about_challenge",
        "human_review": "human_review",
        "fail_run": "fail_run",
    })
    workflow.add_edge("execute_experiment", "summarize_observation")
    workflow.add_edge("summarize_observation", "update_hypotheses")
    workflow.add_edge("update_hypotheses", "verify_candidates")
    router = partial(after_verify, **budget_kwargs)
    workflow.add_conditional_edges("verify_candidates", router, {
        "finish_run": "finish_run", "human_review": "human_review", "fail_run": "fail_run",
        "reason_about_challenge": "reason_about_challenge",
    })
    workflow.add_conditional_edges("human_review", after_human_review, {"end": END})
    workflow.add_edge("finish_run", END)
    workflow.add_edge("fail_run", END)
    return workflow.compile(checkpointer=checkpointer or MemorySaver(), interrupt_before=["human_review"])


def _budgeted_fail_run(state: WorkflowState, **budget_kwargs: int) -> dict[str, Any]:
    result = fail_run(state)
    if state.get("failure_reason"):
        return result
    diagnostic = budget_diagnostic(state, **budget_kwargs)
    if diagnostic is None:
        return result
    event = {"kind": "budget-exhausted", **diagnostic, "at": utc_now()}
    return {
        **result,
        "failure_reason": (
            f"Graph budget exhausted: budget_type={diagnostic['budget_type']} "
            f"configured_limit={diagnostic['configured_limit']} current_value={diagnostic['current_value']} "
            "route=fail_run"
        ),
        "events": [*result.get("events", []), event],
    }
