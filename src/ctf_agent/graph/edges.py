"""Budgeted conditional routing for the CTF LangGraph workflow."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from ctf_agent.graph.state import WorkflowState

Route = Literal["finish_run", "human_review", "fail_run", "reason_about_challenge", "select_experiment"]
SelectRoute = Literal["execute_experiment", "reason_about_challenge", "human_review", "fail_run"]


def after_verify(
    state: WorkflowState,
    *,
    max_tool_calls: int | None = None,
    max_network_requests: int = 12,
    max_total_seconds: int = 1800,
    max_repeated_actions: int = 3,
    max_consecutive_failures: int = 3,
) -> Route:
    """Choose the only post-verification route; never mutates state."""
    if state.get("solved") or state.get("verified_candidates"):
        return "finish_run"
    if state.get("paused") or _needs_human(state):
        return "human_review"
    if state.get("failure_reason") or state.get("iteration", 0) >= state.get("max_iterations", 1):
        return "fail_run"
    tool_limit = max_tool_calls if max_tool_calls is not None else state.get("max_iterations", 1)
    if len(state.get("tool_calls", [])) >= tool_limit:
        return "fail_run"
    if _network_requests(state) >= max_network_requests or _elapsed_seconds(state) >= max_total_seconds:
        return "fail_run"
    if _repeated_action(state) >= max_repeated_actions:
        return "fail_run"
    if _consecutive_failures(state) >= max_consecutive_failures:
        return "reason_about_challenge"
    if state.get("unknowns"):
        return "select_experiment"
    return "reason_about_challenge"


def after_select_experiment(state: WorkflowState) -> SelectRoute:
    """Route after deterministic experiment policy assessment; never mutates state."""
    if state.get("failure_reason"):
        return "fail_run"
    if state.get("paused"):
        return "human_review"
    if state.get("replan_required"):
        return "reason_about_challenge"
    if not state.get("selected_experiment") or state.get("phase") != "experiment-selected":
        return "reason_about_challenge"
    return "execute_experiment"


def after_human_review(_: WorkflowState) -> Literal["end"]:
    return "end"


def _needs_human(state: WorkflowState) -> bool:
    for event in reversed(state.get("events", [])):
        decision = event.get("decision") if isinstance(event, dict) else None
        if isinstance(decision, dict):
            return bool(decision.get("need_human"))
    return False


def _network_requests(state: WorkflowState) -> int:
    return sum(call.get("action_type") == "http_request" for call in state.get("tool_calls", []) if isinstance(call, dict))


def _repeated_action(state: WorkflowState) -> int:
    calls = [call.get("action_type") for call in state.get("tool_calls", []) if isinstance(call, dict)]
    if not calls:
        return 0
    latest, count = calls[-1], 0
    for action in reversed(calls):
        if action != latest:
            break
        count += 1
    return count


def _consecutive_failures(state: WorkflowState) -> int:
    return len(state.get("failed_actions", []))


def _elapsed_seconds(state: WorkflowState) -> float:
    timestamps = [event.get("at") for event in state.get("events", []) if isinstance(event, dict) and event.get("at")]
    if not timestamps:
        return 0.0
    try:
        started = datetime.fromisoformat(str(timestamps[0]).replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    except ValueError:
        return 0.0


# Initial scaffold compatibility.
def after_reason(state: WorkflowState) -> Literal["execute", "finalize"]:
    return "finalize" if state.get("paused") or state.get("failure_reason") else "execute"
