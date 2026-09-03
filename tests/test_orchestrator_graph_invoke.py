import asyncio
from pathlib import Path

import pytest

from ctf_agent.agents import AgentContext
from ctf_agent.core.models import Challenge
from ctf_agent.core.orchestrator import Orchestrator
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.graph import nodes as graph_nodes
from ctf_agent.graph.state import WorkflowState, initial_workflow_state
from ctf_agent.pydantic_agent.agent import PydanticAISolverReasoner, SolverDependencies
from ctf_agent.pydantic_agent.models import SolverDecision
from ctf_agent.sandbox import ExecutionResult, Executor
from ctf_agent.tools import default_registry


class RecordingExecutor(Executor):
    def __init__(self) -> None:
        self.calls = 0

    def run(self, command: str, cwd: str | Path, timeout: int | None = None, env: dict[str, str] | None = None) -> ExecutionResult:
        self.calls += 1
        raise AssertionError("executor must not be called by graph pause invoke")


def _pause_decision() -> dict[str, object]:
    return {
        "current_hypothesis": {
            "name": "pause",
            "claim": "Human input is required before execution.",
            "evidence_for": [],
            "evidence_against": [],
            "confidence": 0.4,
            "falsification_test": "Ask for the missing scope.",
        },
        "confirmed_facts": [],
        "unknowns": ["scope"],
        "candidate_chains": [],
        "selected_experiment": {
            "goal": "Pause for human scope confirmation.",
            "action_type": "pause",
            "action_input": {"type": "pause", "reason": "Need human scope confirmation."},
            "expected_signal": "human",
            "failure_signal": "no input",
            "risk": "low",
            "rollback": "none",
        },
        "next_action": "execute_selected_experiment",
        "need_human": False,
        "stop_reason": None,
    }


def _reasoner(decision: dict[str, object] | None = None) -> PydanticAISolverReasoner:
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    return PydanticAISolverReasoner(
        agent=Agent(
            TestModel(custom_output_args=decision or _pause_decision()),
            deps_type=SolverDependencies,
            output_type=SolverDecision,
            instructions="test",
        )
    )


class BrokenAgent:
    def run_sync(self, *args, **kwargs):
        raise RuntimeError("Bearer secret-token")


def _context(tmp_path: Path, executor: RecordingExecutor | None = None, challenge_id: str = "graph-invoke") -> AgentContext:
    manager = WorkspaceManager(tmp_path / "workspace")
    challenge = Challenge(id=challenge_id, title="Graph Invoke", category="misc")
    state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    trace = manager.trace_store_for(challenge.id)
    return AgentContext(
        state=state,
        layout=layout,
        trace_store=trace,
        executor=executor or RecordingExecutor(),
        tool_registry=default_registry(),
        config={"workspace_dir": str(tmp_path / "workspace")},
        max_steps=3,
        timeout=7,
        metadata={"memory_matches": [{"id": "m"}], "relevant_skill_notes": [{"note": "s"}]},
    )


def test_invoke_graph_workflow_pauses_without_executor_network_verifier_or_llm_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    verifier_calls = {"count": 0}
    llm_loop_calls = {"count": 0}
    http_calls = {"count": 0}

    def verifier_run(*args, **kwargs):
        verifier_calls["count"] += 1
        raise AssertionError("verifier must not be called before graph pause interrupt")

    class ForbiddenLLMActionLoop:
        def __init__(self, *args, **kwargs) -> None:
            llm_loop_calls["count"] += 1
            raise AssertionError("LLMActionLoop must not be constructed by graph invoke")

    def http_request(*args, **kwargs):
        http_calls["count"] += 1
        raise AssertionError("http_request must not be called by pause decision")

    import ctf_agent.core.orchestrator as orchestrator_module
    import ctf_agent.pydantic_agent.tools as tool_module

    monkeypatch.setattr(orchestrator_module, "LLMActionLoop", ForbiddenLLMActionLoop)
    monkeypatch.setattr(tool_module.VerifierAgent, "run", verifier_run)
    monkeypatch.setattr(graph_nodes, "http_request", http_request)

    executor = RecordingExecutor()
    context = _context(tmp_path, executor)
    orchestrator = Orchestrator(
        {"workspace_dir": str(tmp_path / "workspace")},
        brain="graph",
        graph_reasoner=_reasoner(),
    )

    try:
        final_state: WorkflowState = orchestrator._invoke_graph_workflow(context)
        assert isinstance(final_state, dict)
        assert set(initial_workflow_state(context.state.challenge, run_dir=context.layout.challenge_dir)).issubset(final_state)
        assert final_state["challenge"]["id"] == context.state.challenge.id
        assert final_state["run_dir"] == str(context.layout.challenge_dir)
        assert final_state["iteration"] == 1
        assert final_state["max_iterations"] == context.max_steps
        assert final_state["paused"] is True
        assert any(call.get("action_type") == "pause" and call.get("status") == "paused" for call in final_state["tool_calls"])
        assert any(observation.get("source") == "pause_for_human" for observation in final_state["observations"])
        assert executor.calls == 0
        assert verifier_calls["count"] == 0
        assert llm_loop_calls["count"] == 0
        assert http_calls["count"] == 0
        assert str(context.layout.challenge_dir.expanduser()) not in graph_nodes._RUNTIMES

        other_context = _context(tmp_path, challenge_id="graph-invoke-other")
        other_state = initial_workflow_state(other_context.state.challenge, run_dir=other_context.layout.challenge_dir)
        fallback_patch = graph_nodes.reason_about_challenge(other_state)
        assert fallback_patch["phase"] == "reasoned"
        assert fallback_patch["events"][0]["decision"]["stop_reason"] == "No reasoning provider bound."
        assert str(other_context.layout.challenge_dir.expanduser()) not in graph_nodes._RUNTIMES
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_invoke_graph_workflow_missing_provider_fails_without_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CTF_AGENT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    context = _context(tmp_path)
    orchestrator = Orchestrator({"workspace_dir": str(tmp_path / "workspace")}, brain="graph")

    with pytest.raises(RuntimeError, match="graph mode requires a configured PydanticAI provider"):
        orchestrator._invoke_graph_workflow(context)

    assert str(context.layout.challenge_dir.expanduser()) not in graph_nodes._RUNTIMES


def test_invoke_graph_workflow_graph_error_fails_safely_without_fallback(tmp_path: Path):
    context = _context(tmp_path)
    orchestrator = Orchestrator(
        {"workspace_dir": str(tmp_path / "workspace")},
        brain="graph",
        graph_reasoner=PydanticAISolverReasoner(agent=BrokenAgent()),
    )

    with pytest.raises(RuntimeError, match="graph workflow invocation failed without fallback") as exc:
        orchestrator._invoke_graph_workflow(context)

    assert "secret-token" not in str(exc.value)
    assert str(context.layout.challenge_dir.expanduser()) not in graph_nodes._RUNTIMES
