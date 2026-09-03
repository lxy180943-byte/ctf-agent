import shutil
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
    challenge = Challenge(id="inspect-binary", title="binary", category="reverse")
    challenge_state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    trace = manager.trace_store_for(challenge.id)
    context = AgentContext(state=challenge_state, layout=layout, trace_store=trace, executor=LocalExecutor(manager.workspace_root, trace_store=trace, challenge_id=challenge.id), tool_registry=default_registry(), config={}, max_steps=1, timeout=5)
    bind_runtime(layout.challenge_dir, NodeRuntime(tools=ToolDependencies(context=context)))
    return initial_workflow_state(challenge, run_dir=layout.challenge_dir), layout


def _experiment(path: str):
    return {"id": "e-binary", "safety_checked": True, "safety_reason": "graph gate passed", "action_type": "inspect_binary", "plan": {"goal": "Inspect local binary", "action_type": "inspect_binary", "action_input": {"type": "inspect_binary", "path": path}, "expected_signal": "ELF", "failure_signal": "missing", "risk": "low", "rollback": "none"}}


def test_inspect_binary_runs_once_and_records_structured_evidence(tmp_path: Path):
    state, layout = _setup(tmp_path)
    shutil.copyfile("/bin/true", layout.work_dir / "fixture")
    state["experiments"] = [_experiment("work/fixture")]
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(layout.challenge_dir)
    observation = result["observations"][0]
    assert observation["source"] == "inspect_binary"
    assert "ELF" in observation["evidence"]["binary"]["file_type"]
    assert observation["evidence"]["binary"]["format"] == "ELF"
    call = result["tool_calls"][0]
    assert call["status"] == "executed"
    assert {"goal", "action_type", "action_input", "risk_decision", "expected_signal", "failure_signal", "expected_signal_matched", "failure_signal_matched", "duration_seconds", "artifact_paths"} <= set(call)
    assert state["solved"] is False


def test_inspect_binary_outside_workspace_is_blocked_without_tool_call(tmp_path: Path, monkeypatch):
    state, layout = _setup(tmp_path)
    state["experiments"] = [_experiment("../../outside")]
    import ctf_agent.graph.nodes as nodes
    monkeypatch.setattr(nodes, "inspect_binary", lambda *args: (_ for _ in ()).throw(AssertionError("unexpected tool call")))
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(layout.challenge_dir)
    assert result["tool_calls"][0]["status"] == "blocked"
    assert result["failed_actions"]
    assert state["solved"] is False


def test_inspect_binary_tool_exception_is_recorded(tmp_path: Path, monkeypatch):
    state, layout = _setup(tmp_path)
    target = layout.work_dir / "fixture"
    target.write_bytes(b"not an ELF")
    state["experiments"] = [_experiment("work/fixture")]
    import ctf_agent.graph.nodes as nodes
    monkeypatch.setattr(nodes, "inspect_binary", lambda *args: (_ for _ in ()).throw(RuntimeError("tool unavailable")))
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(layout.challenge_dir)
    assert result["tool_calls"][0]["status"] == "failed"
    assert result["failed_actions"]
    assert state["solved"] is False
