from pathlib import Path

import pytest

from ctf_agent.agents import AgentContext
from ctf_agent.core.models import Challenge
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.graph.nodes import NodeRuntime, bind_runtime, clear_runtime, execute_experiment
from ctf_agent.graph.state import initial_workflow_state
from ctf_agent.pydantic_agent.tools import ToolDependencies, ToolObservation
from ctf_agent.sandbox import LocalExecutor
from ctf_agent.tools import default_registry


def _setup(tmp_path: Path):
    manager = WorkspaceManager(tmp_path / "workspace")
    challenge = Challenge(id="http-request", title="http", category="web", connection="http://challenge.local:8080")
    challenge_state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    trace = manager.trace_store_for(challenge.id)
    context = AgentContext(
        state=challenge_state, layout=layout, trace_store=trace,
        executor=LocalExecutor(manager.workspace_root, trace_store=trace, challenge_id=challenge.id),
        tool_registry=default_registry(), config={"sandbox": {"allow_network": True}}, max_steps=1, timeout=5,
    )
    bind_runtime(layout.challenge_dir, NodeRuntime(tools=ToolDependencies(context=context)))
    return initial_workflow_state(challenge, run_dir=layout.challenge_dir), layout


def _experiment(url: str, headers: dict[str, str] | None = None):
    return {"id": "e-http", "safety_checked": True, "safety_reason": "graph gate passed", "action_type": "http_request", "plan": {"goal": "Collect authorized HTTP evidence", "action_type": "http_request", "action_input": {"type": "http_request", "method": "GET", "url": url, "params": {"q": "one"}, "headers": headers or {}, "body": None, "timeout": 5}, "expected_signal": "Evidence", "failure_signal": "missing", "risk": "medium", "rollback": "none"}}


def _result(*, ok: bool = True, error: str | None = None) -> ToolObservation:
    return ToolObservation(tool="http_request", ok=ok, risk="medium", duration_seconds=0.01, observation={"status": 200, "headers": {"content-type": "text/html"}, "title": "Evidence", "forms": [], "links": [], "scripts": [], "body_excerpt": "Evidence"}, artifacts=[], error=error)


def test_authorized_http_request_uses_structured_tool_once(tmp_path: Path, monkeypatch):
    state, layout = _setup(tmp_path)
    state["experiments"] = [_experiment("http://challenge.local:8080/index?q=one", {"Authorization": "Bearer secret-value", "X-Test": "ok"})]
    calls = []
    import ctf_agent.graph.nodes as nodes
    monkeypatch.setattr(nodes, "http_request", lambda deps, request: calls.append(request) or _result())
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(layout.challenge_dir)
    assert len(calls) == 1
    assert result["observations"][0]["evidence"]["status"] == 200
    call = result["tool_calls"][0]
    assert call["status"] == "executed"
    assert call["action_input"]["headers"]["Authorization"] == "<redacted>"
    assert "secret-value" not in layout.trace_path.read_text(encoding="utf-8")
    assert state["solved"] is False


@pytest.mark.parametrize("url", ["http://other.local:8080/", "http://challenge.local:8081/"])
def test_mismatched_host_or_port_is_blocked_without_tool_call(tmp_path: Path, monkeypatch, url: str):
    state, layout = _setup(tmp_path)
    state["experiments"] = [_experiment(url)]
    import ctf_agent.graph.nodes as nodes
    monkeypatch.setattr(nodes, "http_request", lambda *args: pytest.fail("unexpected request"))
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(layout.challenge_dir)
    assert result["tool_calls"][0]["status"] == "blocked"
    assert result["failed_actions"]
    assert state["solved"] is False


def test_unauthorized_localhost_is_blocked_without_tool_call(tmp_path: Path, monkeypatch):
    state, layout = _setup(tmp_path)
    state["experiments"] = [_experiment("http://127.0.0.1:8080/")]
    import ctf_agent.graph.nodes as nodes
    monkeypatch.setattr(nodes, "http_request", lambda *args: pytest.fail("unexpected request"))
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(layout.challenge_dir)
    assert result["tool_calls"][0]["status"] == "blocked"
    assert state["solved"] is False


def test_timeout_or_tool_exception_is_recorded(tmp_path: Path, monkeypatch):
    state, layout = _setup(tmp_path)
    state["experiments"] = [_experiment("http://challenge.local:8080/")]
    import ctf_agent.graph.nodes as nodes
    monkeypatch.setattr(nodes, "http_request", lambda *args: _result(ok=False, error="timed out"))
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(layout.challenge_dir)
    assert result["tool_calls"][0]["status"] == "failed"
    assert result["failed_actions"]
    assert result["observations"][0]["evidence"]["title"] == "Evidence"
    assert state["solved"] is False
