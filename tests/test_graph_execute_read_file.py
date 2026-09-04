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
    challenge = Challenge(id="read-file", title="read", category="misc")
    state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    target = layout.work_dir / "a.txt"
    target.write_text("known text", encoding="utf-8")
    trace = manager.trace_store_for(challenge.id)
    context = AgentContext(state=state, layout=layout, trace_store=trace, executor=LocalExecutor(manager.workspace_root, trace_store=trace, challenge_id=challenge.id), tool_registry=default_registry(), config={}, max_steps=1, timeout=5)
    bind_runtime(layout.challenge_dir, NodeRuntime(tools=ToolDependencies(context=context)))
    return initial_workflow_state(challenge, run_dir=layout.challenge_dir), layout


def _experiment(expected_signal: str = "known text"):
    return {"id": "e-read", "safety_checked": True, "safety_reason": "low risk", "action_type": "read_file", "plan": {"goal": "Read evidence", "action_type": "read_file", "action_input": {"type": "read_file", "path": "a.txt"}, "expected_signal": expected_signal, "failure_signal": "missing", "risk": "low", "rollback": "none"}}


def test_read_file_dispatch_is_structured_and_auditable(tmp_path: Path):
    state, layout = _setup(tmp_path)
    state["experiments"] = [_experiment()]
    result = execute_experiment(state)
    clear_runtime(layout.challenge_dir)
    assert result["observations"][0]["source"] == "read_file"
    assert "known text" in result["observations"][0]["evidence"]["body_excerpt"]
    call = result["tool_calls"][0]
    assert call["status"] == "executed"
    assert call["action_input"]["type"] == "read_file"
    assert call["expected_signal_matched"] is True
    assert call["failure_signal_matched"] is False
    assert call["artifact_paths"]
    assert state["solved"] is False
    assert "LLMActionLoop" not in layout.trace_path.read_text(encoding="utf-8")


def test_candidate_discovery_matches_natural_language_expected_signal_without_solving(tmp_path: Path):
    state, layout = _setup(tmp_path)
    (layout.work_dir / "a.txt").write_text("metadata comment flag{candidate_only}", encoding="utf-8")
    state["experiments"] = [_experiment("file contents include a substring matching the flag regex")]

    result = execute_experiment(state)
    clear_runtime(layout.challenge_dir)

    call = result["tool_calls"][0]
    assert call["status"] == "executed"
    assert call["expected_signal_matched"] is True
    assert state["solved"] is False
    assert "verified_candidates" not in result


def test_candidate_text_does_not_match_unrelated_binary_expected_signal(tmp_path: Path):
    state, layout = _setup(tmp_path)
    (layout.work_dir / "a.txt").write_text("metadata comment flag{candidate_only}", encoding="utf-8")
    state["experiments"] = [_experiment("ELF format")]

    result = execute_experiment(state)
    clear_runtime(layout.challenge_dir)

    call = result["tool_calls"][0]
    assert call["status"] == "executed"
    assert call["expected_signal_matched"] is False
    assert state["solved"] is False
    assert "verified_candidates" not in result


def test_successful_read_without_expected_signal_remains_unmatched(tmp_path: Path):
    state, layout = _setup(tmp_path)
    (layout.work_dir / "a.txt").write_text("ordinary text with no requested marker", encoding="utf-8")
    state["experiments"] = [_experiment("zip archive header")]

    result = execute_experiment(state)
    clear_runtime(layout.challenge_dir)

    call = result["tool_calls"][0]
    assert call["status"] == "executed"
    assert call["expected_signal_matched"] is False


def test_read_file_tool_failure_is_observed_without_raising(tmp_path: Path, monkeypatch):
    state, layout = _setup(tmp_path)
    state["experiments"] = [_experiment()]
    import ctf_agent.graph.nodes as nodes
    monkeypatch.setattr(nodes, "read_file", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("read failed")))
    result = execute_experiment(state)
    clear_runtime(layout.challenge_dir)
    assert result["failed_actions"]
    assert result["observations"][0]["source"] == "read_file"
    assert result["tool_calls"][0]["status"] == "failed"
