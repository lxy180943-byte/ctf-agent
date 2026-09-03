from pathlib import Path

from ctf_agent.agents import AgentContext
from ctf_agent.core.models import Challenge, utc_now
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.graph.nodes import NodeRuntime, bind_runtime, clear_runtime, execute_experiment
from ctf_agent.graph.state import initial_workflow_state
from ctf_agent.pydantic_agent.tools import ToolDependencies
from ctf_agent.sandbox import ExecutionResult, LocalExecutor
from ctf_agent.tools import default_registry


def _setup(tmp_path: Path):
    manager = WorkspaceManager(tmp_path / "workspace")
    challenge = Challenge(id="ask-verifier", title="verify", category="misc")
    challenge_state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    trace = manager.trace_store_for(challenge.id)
    context = AgentContext(state=challenge_state, layout=layout, trace_store=trace, executor=LocalExecutor(manager.workspace_root, trace_store=trace, challenge_id=challenge.id), tool_registry=default_registry(), config={}, max_steps=1, timeout=5)
    deps = ToolDependencies(context=context)
    bind_runtime(layout.challenge_dir, NodeRuntime(tools=deps))
    return initial_workflow_state(challenge, run_dir=layout.challenge_dir), layout, deps


def _experiment():
    return {"id": "e-verify", "safety_checked": True, "safety_reason": "low risk", "action_type": "ask_verifier", "plan": {"goal": "Verify observed candidates", "action_type": "ask_verifier", "action_input": {"type": "ask_verifier"}, "expected_signal": "candidate_count", "failure_signal": "verifier failed", "risk": "low", "rollback": "none"}}


def test_ask_verifier_returns_candidate_observation_without_solving(tmp_path: Path):
    state, layout, deps = _setup(tmp_path)
    now = utc_now()
    deps.execution_batch.results.append(ExecutionResult(command="fixture", cwd=str(layout.work_dir), env={}, timeout=1, exit_code=0, stdout="flag{observed}", stderr="", started_at=now, ended_at=now, duration_seconds=0.0))
    state["experiments"] = [_experiment()]
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(layout.challenge_dir)
    assert result["observations"][0]["evidence"]["candidate_count"] == 1
    assert result["tool_calls"][0]["status"] == "executed"
    assert state["solved"] is False and state["verified_candidates"] == []
    assert "LLMActionLoop" not in layout.trace_path.read_text(encoding="utf-8")


def test_ask_verifier_without_candidates_is_structured(tmp_path: Path):
    state, layout, _ = _setup(tmp_path)
    state["experiments"] = [_experiment()]
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(layout.challenge_dir)
    assert result["observations"][0]["evidence"]["candidate_count"] == 0
    assert result["tool_calls"][0]["status"] == "executed"
    assert state["verified_candidates"] == []


def test_ask_verifier_exception_is_recorded(tmp_path: Path, monkeypatch):
    state, layout, _ = _setup(tmp_path)
    state["experiments"] = [_experiment()]
    import ctf_agent.graph.nodes as nodes
    monkeypatch.setattr(nodes, "ask_verifier", lambda *args: (_ for _ in ()).throw(RuntimeError("verifier failed")))
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(layout.challenge_dir)
    assert result["failed_actions"] and result["tool_calls"][0]["status"] == "failed"
    assert state["solved"] is False and state["verified_candidates"] == []
