import json
from pathlib import Path

import pytest

from ctf_agent.core.models import Challenge
from ctf_agent.core.orchestrator import Orchestrator
from ctf_agent.core.state import ChallengeState
from ctf_agent.graph.state import initial_workflow_state
from ctf_agent.pydantic_agent.agent import PydanticAISolverReasoner, SolverDependencies
from ctf_agent.pydantic_agent.models import SolverDecision
from ctf_agent.sandbox import ExecutionResult, Executor


class RecordingExecutor(Executor):
    def __init__(self) -> None:
        self.calls = 0

    def run(self, command: str, cwd: str | Path, timeout: int | None = None, env: dict[str, str] | None = None) -> ExecutionResult:
        self.calls += 1
        raise AssertionError("executor must not be called by graph e2e tests")


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


def _reasoner() -> PydanticAISolverReasoner:
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    return PydanticAISolverReasoner(
        agent=Agent(
            TestModel(custom_output_args=_pause_decision()),
            deps_type=SolverDependencies,
            output_type=SolverDecision,
            instructions="test",
        )
    )


@pytest.fixture
def forbid_legacy_paths(monkeypatch: pytest.MonkeyPatch):
    calls = {"llm_loop": 0, "verifier": 0, "http": 0, "initial_plan": 0, "execute_plan": 0}

    class ForbiddenLLMActionLoop:
        def __init__(self, *args, **kwargs) -> None:
            calls["llm_loop"] += 1
            raise AssertionError("LLMActionLoop must not be used by graph branch")

    def forbidden_verifier(*args, **kwargs):
        calls["verifier"] += 1
        raise AssertionError("verifier must not be used before graph pause interrupt")

    def forbidden_http(*args, **kwargs):
        calls["http"] += 1
        raise AssertionError("http_request must not be used by pause decision")

    def forbidden_initial_plan(*args, **kwargs):
        calls["initial_plan"] += 1
        raise AssertionError("_initial_plan must not be used by graph branch")

    def forbidden_execute_plan(*args, **kwargs):
        calls["execute_plan"] += 1
        raise AssertionError("_execute_plan must not be used by graph branch")

    import ctf_agent.core.orchestrator as orchestrator_module
    import ctf_agent.graph.nodes as graph_nodes
    import ctf_agent.pydantic_agent.tools as tool_module

    monkeypatch.setattr(orchestrator_module, "LLMActionLoop", ForbiddenLLMActionLoop)
    monkeypatch.setattr(tool_module.VerifierAgent, "run", forbidden_verifier)
    monkeypatch.setattr(graph_nodes, "ask_verifier", forbidden_verifier)
    monkeypatch.setattr(graph_nodes, "http_request", forbidden_http)
    monkeypatch.setattr(Orchestrator, "_initial_plan", forbidden_initial_plan)
    monkeypatch.setattr(Orchestrator, "_execute_plan", forbidden_execute_plan)
    return calls


def _patch_executor(monkeypatch: pytest.MonkeyPatch) -> RecordingExecutor:
    executor = RecordingExecutor()
    monkeypatch.setattr(Orchestrator, "_build_executor", lambda self, challenge, trace_store: executor)
    return executor


def _trace_actions(run_dir: Path) -> list[str]:
    return [json.loads(line)["action"] for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def test_graph_solve_pause_returns_paused_without_legacy_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forbid_legacy_paths):
    executor = _patch_executor(monkeypatch)
    orchestrator = Orchestrator(
        {"workspace_dir": str(tmp_path / "workspace")},
        brain="graph",
        graph_reasoner=_reasoner(),
    )

    result = orchestrator.solve(Challenge(id="graph-e2e-pause", title="Graph Pause", category="misc"))

    assert result.state is ChallengeState.PAUSED
    assert result.metadata["brain"] == "graph"
    assert result.metadata["brain_mode"] == "graph"
    assert result.metadata["pause_reason"] == "Need human scope confirmation."
    assert result.metadata["pending_human_question"] == "Need human scope confirmation."
    assert result.metadata["next_goal"] == "Pause for human scope confirmation."
    assert "graph-start" in _trace_actions(result.run_dir)
    assert "graph-finish" in _trace_actions(result.run_dir)
    assert executor.calls == 0
    assert all(value == 0 for value in forbid_legacy_paths.values())


def test_graph_provider_missing_fails_without_fallback_and_traces_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forbid_legacy_paths):
    monkeypatch.delenv("CTF_AGENT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _patch_executor(monkeypatch)
    orchestrator = Orchestrator({"workspace_dir": str(tmp_path / "workspace")}, brain="graph")

    result = orchestrator.solve(Challenge(id="graph-e2e-provider", title="Graph Provider", category="misc"))

    assert result.state is ChallengeState.FAILED
    assert result.metadata["brain"] == "graph"
    assert "PydanticAI provider" in result.metadata["graph_failure_reason"]
    assert "graph-error" in _trace_actions(result.run_dir)
    assert forbid_legacy_paths["llm_loop"] == 0
    assert forbid_legacy_paths["initial_plan"] == 0
    assert forbid_legacy_paths["execute_plan"] == 0


def test_graph_unverified_candidate_does_not_mark_solved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forbid_legacy_paths):
    _patch_executor(monkeypatch)

    def fake_invoke(self, context):
        workflow_state = initial_workflow_state(context.state.challenge, run_dir=context.layout.challenge_dir, max_iterations=context.max_steps)
        workflow_state.update(
            {
                "phase": "finished",
                "solved": True,
                "observations": [{"source": "model", "text": "maybe flag{unverified}"}],
                "verified_candidates": [{"value": "flag{unverified}", "source": "model", "confidence": 0.9, "verified": False}],
                "events": [{"node": "reason_about_challenge", "status": "ok"}],
            }
        )
        return workflow_state

    monkeypatch.setattr(Orchestrator, "_invoke_graph_workflow", fake_invoke)
    orchestrator = Orchestrator(
        {"workspace_dir": str(tmp_path / "workspace")},
        brain="graph",
        graph_reasoner=_reasoner(),
    )

    result = orchestrator.solve(Challenge(id="graph-e2e-unverified", title="Graph Unverified", category="misc"))

    assert result.state is ChallengeState.ANALYZING
    assert result.solved is False
    assert result.flags == []
    assert result.metadata["graph_solved"] is False
    assert "graph-finish" in _trace_actions(result.run_dir)
    assert forbid_legacy_paths["initial_plan"] == 0
    assert forbid_legacy_paths["execute_plan"] == 0


def test_resume_paused_graph_run_continues_checkpoint_and_preserves_graph_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _patch_executor(monkeypatch)
    orchestrator = Orchestrator(
        {"workspace_dir": str(tmp_path / "workspace")},
        brain="graph",
        graph_reasoner=_reasoner(),
    )
    first = orchestrator.solve(Challenge(id="graph-e2e-resume", title="Graph Resume", category="misc"))

    resumed = Orchestrator(
        {"workspace_dir": str(tmp_path / "workspace")},
        brain="graph",
        graph_reasoner=_reasoner(),
    ).resume_from_run_dir(first.run_dir)

    assert resumed.state is ChallengeState.PAUSED
    assert resumed.metadata["resumed"] is True
    assert resumed.metadata["graph_checkpoint_found"] is True
    assert resumed.metadata["graph_resume_requested"] is True
    assert resumed.metadata["graph_resume_mode"] == "resumed"
    assert resumed.metadata["graph"]["pause_reason"] == "Need human scope confirmation."
    assert resumed.metadata["pause_reason"] == "Need human scope confirmation."
