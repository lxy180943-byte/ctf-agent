from __future__ import annotations

from pathlib import Path

from langgraph.graph import END, START, StateGraph

from ctf_agent.graph.checkpoint import graph_thread_id, open_run_checkpointer

from ctf_agent.agents import AgentContext
from ctf_agent.core.models import Challenge
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.graph.edges import _consecutive_failures, after_verify
from ctf_agent.graph.nodes import NodeRuntime, bind_runtime, clear_runtime, execute_experiment
from ctf_agent.graph.state import WorkflowState, initial_workflow_state, restore_workflow_state, serialize_workflow_state
from ctf_agent.pydantic_agent.tools import ToolDependencies
from ctf_agent.sandbox import LocalExecutor
from ctf_agent.tools import default_registry


def _state(tmp_path: Path):
    return initial_workflow_state({"id": "budget", "title": "Budget", "category": "misc"}, run_dir=tmp_path / "run", max_iterations=10)


def _read_call(path: str, *, status: str = "executed", failure: bool = False, timed_out: bool = False) -> dict:
    return {
        "action_type": "read_file",
        "action_input": {"type": "read_file", "path": path},
        "status": status,
        "failure_signal_matched": failure,
        "timed_out": timed_out,
    }


def test_repeated_action_budget_uses_input_aware_fingerprint(tmp_path: Path):
    state = _state(tmp_path)
    state["tool_calls"] = [_read_call("a.txt"), _read_call("b.txt"), _read_call("c.txt")]

    assert after_verify(state, max_tool_calls=10, max_repeated_actions=3) == "reason_about_challenge"

    state["tool_calls"] = [_read_call("a.txt"), _read_call("./a.txt"), _read_call("input/../a.txt")]

    assert after_verify(state, max_tool_calls=10, max_repeated_actions=3) == "fail_run"


def test_repeated_action_streak_is_consecutive_not_cumulative_and_threshold_is_inclusive(tmp_path: Path):
    state = _state(tmp_path)
    state["tool_calls"] = [_read_call("a.txt"), _read_call("b.txt"), _read_call("a.txt")]

    assert after_verify(state, max_tool_calls=10, max_repeated_actions=2) == "reason_about_challenge"

    state["tool_calls"] = [_read_call("a.txt")]
    assert after_verify(state, max_tool_calls=10, max_repeated_actions=2) == "reason_about_challenge"

    state["tool_calls"] = [_read_call("a.txt"), _read_call("./a.txt")]
    assert after_verify(state, max_tool_calls=10, max_repeated_actions=2) == "fail_run"

    state["tool_calls"] = [_read_call("a.txt"), _read_call("./a.txt"), _read_call("foo/../a.txt")]
    assert after_verify(state, max_tool_calls=10, max_repeated_actions=2) == "fail_run"


def test_repetition_is_independent_of_tool_result_status(tmp_path: Path):
    state = _state(tmp_path)
    state["tool_calls"] = [
        _read_call("a.txt", status="executed"),
        _read_call("./a.txt", status="failed", failure=True),
        _read_call("foo/../a.txt", status="executed"),
    ]

    assert after_verify(state, max_tool_calls=10, max_repeated_actions=3) == "fail_run"


def test_consecutive_failure_streak_is_derived_from_recent_tool_calls_and_resets_on_success(tmp_path: Path):
    state = _state(tmp_path)
    state["failed_actions"] = [{"reason": "old failure"}, {"reason": "another old failure"}]
    state["tool_calls"] = [
        _read_call("a.txt", status="failed"),
        _read_call("b.txt", status="executed"),
        _read_call("c.txt", status="failed", failure=True),
    ]

    assert _consecutive_failures(state) == 1

    state["tool_calls"].append(_read_call("d.txt", status="failed", failure=True))

    assert _consecutive_failures(state) == 2


def test_consecutive_failure_threshold_is_inclusive_and_pause_does_not_count(tmp_path: Path):
    state = _state(tmp_path)
    state["tool_calls"] = [_read_call("a.txt", status="failed", failure=True), _read_call("b.txt", status="blocked")]

    assert after_verify(state, max_tool_calls=10, max_repeated_actions=10, max_consecutive_failures=3) == "reason_about_challenge"
    assert after_verify(state, max_tool_calls=10, max_repeated_actions=10, max_consecutive_failures=2) == "fail_run"

    state["tool_calls"].append(_read_call("c.txt", status="paused"))
    assert _consecutive_failures(state) == 0
    assert after_verify(state, max_tool_calls=10, max_repeated_actions=10, max_consecutive_failures=2) == "reason_about_challenge"

    state["paused"] = True
    assert after_verify(state, max_tool_calls=10, max_repeated_actions=10, max_consecutive_failures=2) == "human_review"


def test_error_and_rejected_tool_statuses_count_as_failures(tmp_path: Path):
    state = _state(tmp_path)
    state["tool_calls"] = [_read_call("a.txt", status="error"), _read_call("b.txt", status="rejected")]

    assert _consecutive_failures(state) == 2
    assert after_verify(state, max_tool_calls=10, max_repeated_actions=10, max_consecutive_failures=2) == "fail_run"


def test_budget_counts_survive_restore_and_sqlite_checkpoint_resume(tmp_path: Path):
    state = _state(tmp_path)
    state["tool_calls"] = [_read_call("a.txt"), _read_call("b.txt")]
    restored = restore_workflow_state(serialize_workflow_state(state))
    restored["tool_calls"].append(_read_call("c.txt"))

    assert after_verify(restored, max_tool_calls=3, max_repeated_actions=10) == "fail_run"

    run_dir = tmp_path / "checkpoint-run"
    thread_id = graph_thread_id(str(run_dir))
    config = {"configurable": {"thread_id": thread_id}}
    with open_run_checkpointer(run_dir) as checkpointer:
        graph = StateGraph(WorkflowState)
        graph.add_node("add", lambda workflow_state: {"tool_calls": [_read_call("c.txt")]})
        graph.add_edge(START, "add")
        graph.add_edge("add", END)
        workflow = graph.compile(checkpointer=checkpointer)
        checkpointed = workflow.invoke(state, config=config)

    with open_run_checkpointer(run_dir) as checkpointer:
        checkpoint = checkpointer.get_tuple(config)
        assert checkpoint is not None
        values = checkpoint.checkpoint["channel_values"]
        assert len(values["tool_calls"]) == 3
        assert after_verify(values, max_tool_calls=3, max_repeated_actions=10) == "fail_run"
        assert checkpointed["tool_calls"] == values["tool_calls"]


def _context(tmp_path: Path, challenge: Challenge) -> AgentContext:
    manager = WorkspaceManager(tmp_path / "workspace")
    run_state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    trace = manager.trace_store_for(challenge.id)
    return AgentContext(
        state=run_state,
        layout=layout,
        trace_store=trace,
        executor=LocalExecutor(manager.workspace_root, trace_store=trace, challenge_id=challenge.id),
        tool_registry=default_registry(),
        config={},
        max_steps=2,
        timeout=5,
    )


def _experiment() -> dict:
    return {
        "id": "experiment-1",
        "safety_checked": True,
        "safety_reason": "low risk",
        "action_type": "read_file",
        "plan": {
            "goal": "Read evidence",
            "action_type": "read_file",
            "action_input": {"type": "read_file", "path": "a.txt"},
            "expected_signal": "known text",
            "failure_signal": "missing",
            "risk": "low",
            "rollback": "none",
        },
        "completed": False,
        "status": "planned",
    }


def test_execute_completion_updates_experiment_with_real_workflow_reducer(tmp_path: Path):
    challenge = Challenge(id="budget-execute", title="Budget execute", category="misc")
    context = _context(tmp_path, challenge)
    context.layout.work_dir.mkdir(parents=True, exist_ok=True)
    (context.layout.work_dir / "a.txt").write_text("known text", encoding="utf-8")
    state = initial_workflow_state(challenge, run_dir=context.layout.challenge_dir, max_iterations=2)

    graph = StateGraph(WorkflowState)
    graph.add_node("plan", lambda _state: {"experiments": [_experiment()]})
    graph.add_node("execute", execute_experiment)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "execute")
    graph.add_edge("execute", END)
    workflow = graph.compile()

    bind_runtime(context.layout.challenge_dir, NodeRuntime(tools=ToolDependencies(context=context)))
    try:
        final = workflow.invoke(state)
    finally:
        clear_runtime(context.layout.challenge_dir)

    assert len(final["experiments"]) == 1
    experiment = final["experiments"][0]
    assert experiment["id"] == "experiment-1"
    assert experiment["completed"] is True
    assert experiment["status"] == "completed"
    assert experiment["outcome"] == "executed"
    assert experiment["tool_call_id"] == "tool-call-experiment-1"
    restored = restore_workflow_state(serialize_workflow_state(final))
    assert restored["experiments"] == final["experiments"]
