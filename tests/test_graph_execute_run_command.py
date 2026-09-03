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
    challenge = Challenge(id="run-command", title="command", category="misc")
    challenge_state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    trace = manager.trace_store_for(challenge.id)
    context = AgentContext(
        state=challenge_state, layout=layout, trace_store=trace,
        executor=LocalExecutor(manager.workspace_root, trace_store=trace, challenge_id=challenge.id),
        tool_registry=default_registry(), config={}, max_steps=1, timeout=5,
    )
    bind_runtime(layout.challenge_dir, NodeRuntime(tools=ToolDependencies(context=context)))
    return initial_workflow_state(challenge, run_dir=layout.challenge_dir), layout


def _experiment(command: str, expected_signal: str = "local signal"):
    return {
        "id": "e-command", "safety_checked": True, "safety_reason": "graph gate passed",
        "action_type": "run_command",
        "plan": {
            "goal": "Inspect workspace evidence", "action_type": "run_command",
            "action_input": {"type": "run_command", "command": command, "timeout": 5},
            "expected_signal": expected_signal, "failure_signal": "missing signal",
            "risk": "low", "rollback": "none",
        },
    }


def test_run_command_executes_once_with_structured_audit(tmp_path: Path):
    state, layout = _setup(tmp_path)
    (layout.work_dir / "evidence.txt").write_text("local signal", encoding="utf-8")
    state["experiments"] = [_experiment("cat evidence.txt")]
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(layout.challenge_dir)
    call = result["tool_calls"][0]
    assert result["observations"][0]["source"] == "run_command"
    assert "local signal" in result["observations"][0]["evidence"]["body_excerpt"]
    assert call["status"] == "executed"
    assert call["risk_decision"]["level"] == "low"
    assert call["action_input"]["command"] == "cat evidence.txt"
    assert state["solved"] is False
    assert "LLMActionLoop" not in layout.trace_path.read_text(encoding="utf-8")


def test_confirm_required_command_is_blocked_without_deleting_file(tmp_path: Path):
    state, layout = _setup(tmp_path)
    target = layout.work_dir / "known-file"
    target.write_text("keep", encoding="utf-8")
    state["experiments"] = [_experiment("rm -rf ./known-file")]
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(layout.challenge_dir)
    assert result["tool_calls"][0]["status"] == "blocked"
    assert result["failed_actions"]
    assert target.exists()
    assert state["solved"] is False


def test_refused_command_is_blocked_without_execution(tmp_path: Path):
    state, layout = _setup(tmp_path)
    state["experiments"] = [_experiment("sudo id")]
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(layout.challenge_dir)
    assert result["tool_calls"][0]["status"] == "blocked"
    assert result["tool_calls"][0]["risk_decision"]["level"] == "refuse"
    assert result["failed_actions"]
    assert state["solved"] is False


def test_run_command_tool_exception_is_recorded(tmp_path: Path, monkeypatch):
    state, layout = _setup(tmp_path)
    state["experiments"] = [_experiment("cat evidence.txt")]
    import ctf_agent.graph.nodes as nodes
    monkeypatch.setattr(nodes, "run_command", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("executor failed")))
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(layout.challenge_dir)
    assert result["failed_actions"]
    assert result["tool_calls"][0]["status"] == "failed"
    assert result["observations"][0]["source"] == "run_command"
    assert state["solved"] is False
