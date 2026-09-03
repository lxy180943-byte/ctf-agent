import asyncio
from pathlib import Path

import pytest

from ctf_agent.agents import AgentContext
from ctf_agent.core.models import Challenge
from ctf_agent.core.orchestrator import Orchestrator
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.graph.nodes import NodeRuntime
from ctf_agent.graph.state import initial_workflow_state
from ctf_agent.pydantic_agent.agent import PydanticAISolverReasoner, SolverDependencies
from ctf_agent.pydantic_agent.models import SolverDecision
from ctf_agent.pydantic_agent.tools import ToolDependencies
from ctf_agent.sandbox import ExecutionResult, Executor
from ctf_agent.tools import default_registry


class RecordingExecutor(Executor):
    def __init__(self) -> None:
        self.calls = 0

    def run(self, command: str, cwd: str | Path, timeout: int | None = None, env: dict[str, str] | None = None) -> ExecutionResult:
        self.calls += 1
        raise AssertionError("executor must not be called by graph runtime factory")


def _decision() -> dict[str, object]:
    return {
        "current_hypothesis": {
            "name": "read",
            "claim": "Read local evidence.",
            "evidence_for": [],
            "evidence_against": [],
            "confidence": 0.5,
            "falsification_test": "Read file.",
        },
        "confirmed_facts": [],
        "unknowns": ["contents"],
        "candidate_chains": [],
        "selected_experiment": {
            "goal": "Read.",
            "action_type": "read_file",
            "action_input": {"type": "read_file", "path": "work/a.txt"},
            "expected_signal": "text",
            "failure_signal": "error",
            "risk": "low",
            "rollback": "none",
        },
        "next_action": "execute_selected_experiment",
        "need_human": False,
        "stop_reason": None,
    }


def _reasoner() -> PydanticAISolverReasoner:
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    return PydanticAISolverReasoner(
        agent=Agent(
            TestModel(custom_output_args=_decision()),
            deps_type=SolverDependencies,
            output_type=SolverDecision,
            instructions="test",
        )
    )


def _context(tmp_path: Path, executor: RecordingExecutor | None = None) -> AgentContext:
    manager = WorkspaceManager(tmp_path / "workspace")
    challenge = Challenge(id="graph-runtime", title="Graph Runtime", category="misc", connection="http://challenge.local:8080")
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


def test_build_graph_runtime_binds_tools_and_injected_reasoner_without_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    verifier_calls = {"count": 0}
    llm_loop_calls = {"count": 0}

    def verifier_run(*args, **kwargs):
        verifier_calls["count"] += 1
        raise AssertionError("verifier must not be called by graph runtime factory")

    class ForbiddenLLMActionLoop:
        def __init__(self, *args, **kwargs) -> None:
            llm_loop_calls["count"] += 1
            raise AssertionError("LLMActionLoop must not be constructed by graph runtime factory")

    import ctf_agent.core.orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module.VerifierAgent, "run", verifier_run)
    monkeypatch.setattr(orchestrator_module, "LLMActionLoop", ForbiddenLLMActionLoop)

    executor = RecordingExecutor()
    context = _context(tmp_path, executor)
    orchestrator = Orchestrator(
        {"workspace_dir": str(tmp_path / "workspace")},
        brain="graph",
        graph_reasoner=_reasoner(),
    )
    graph_state = initial_workflow_state(context.state.challenge, run_dir=context.layout.challenge_dir)

    try:
        runtime = orchestrator._build_graph_runtime(context)
        assert isinstance(runtime, NodeRuntime)
        assert isinstance(runtime.tools, ToolDependencies)
        assert runtime.tools.context is context
        assert runtime.reasoner is not None
        result = runtime.reasoner(graph_state)
        assert isinstance(result, SolverDecision)
        assert executor.calls == 0
        assert verifier_calls["count"] == 0
        assert llm_loop_calls["count"] == 0
        assert context.state.state.value == "new"
        assert context.state.flag_candidates == []
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_build_graph_runtime_missing_provider_fails_without_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CTF_AGENT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    context = _context(tmp_path)
    orchestrator = Orchestrator({"workspace_dir": str(tmp_path / "workspace")}, brain="graph")

    with pytest.raises(RuntimeError, match="graph mode requires a configured PydanticAI provider"):
        orchestrator._build_graph_runtime(context)

    assert orchestrator.graph_reasoner is None
