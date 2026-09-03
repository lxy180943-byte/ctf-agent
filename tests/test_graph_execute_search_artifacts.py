from pathlib import Path

from ctf_agent.agents import AgentContext
from ctf_agent.core.models import Challenge
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.graph.nodes import NodeRuntime, bind_runtime, clear_runtime, execute_experiment
from ctf_agent.graph.state import initial_workflow_state
from ctf_agent.pydantic_agent.tools import ToolDependencies
from ctf_agent.sandbox import LocalExecutor
from ctf_agent.tools import default_registry


def _setup(tmp_path: Path):
    manager = WorkspaceManager(tmp_path / "workspace")
    challenge = Challenge(id="search-artifacts", title="search", category="misc")
    state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    trace = manager.trace_store_for(challenge.id)
    context = AgentContext(
        state=state, layout=layout, trace_store=trace,
        executor=LocalExecutor(manager.workspace_root, trace_store=trace, challenge_id=challenge.id),
        tool_registry=default_registry(), config={}, max_steps=1, timeout=5,
    )
    bind_runtime(layout.challenge_dir, NodeRuntime(tools=ToolDependencies(context=context)))
    return initial_workflow_state(challenge, run_dir=layout.challenge_dir), layout


def _experiment(pattern: str):
    return {
        "id": "e-search", "safety_checked": True, "safety_reason": "low risk",
        "action_type": "search_artifacts",
        "plan": {
            "goal": "Search collected artifacts", "action_type": "search_artifacts",
            "action_input": {"type": "search_artifacts", "pattern": pattern},
            "expected_signal": pattern, "failure_signal": "not-present",
            "risk": "low", "rollback": "none",
        },
    }


def test_search_artifacts_match_is_structured_and_auditable(tmp_path: Path):
    state, layout = _setup(tmp_path)
    (layout.work_dir / "evidence.txt").write_text("needle in local evidence", encoding="utf-8")
    state["experiments"] = [_experiment("needle")]
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(layout.challenge_dir)
    assert result["observations"][0]["source"] == "search_artifacts"
    assert result["observations"][0]["evidence"]["match_count"] == 1
    call = result["tool_calls"][0]
    assert call["status"] == "executed"
    assert call["action_input"] == {"type": "search_artifacts", "pattern": "needle"}
    assert {"goal", "action_type", "risk_decision", "expected_signal", "failure_signal", "expected_signal_matched", "failure_signal_matched", "duration_seconds", "artifact_paths"} <= set(call)
    assert call["expected_signal_matched"] is True
    assert state["solved"] is False


def test_search_artifacts_no_match_still_returns_observation(tmp_path: Path):
    state, layout = _setup(tmp_path)
    state["experiments"] = [_experiment("absent")]
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(layout.challenge_dir)
    assert result["observations"][0]["evidence"]["match_count"] == 0
    assert result["tool_calls"][0]["status"] == "executed"
    assert state["solved"] is False


def test_search_artifacts_tool_failure_is_recorded(tmp_path: Path, monkeypatch):
    state, layout = _setup(tmp_path)
    state["experiments"] = [_experiment("needle")]
    import ctf_agent.graph.nodes as nodes
    monkeypatch.setattr(nodes, "search_artifacts", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("search failed")))
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(layout.challenge_dir)
    assert result["failed_actions"]
    assert result["observations"][0]["source"] == "search_artifacts"
    assert result["tool_calls"][0]["status"] == "failed"
    assert state["solved"] is False
