import asyncio
import json

import pytest

from ctf_agent.graph.reasoner_adapter import GraphReasonerAdapter
from ctf_agent.pydantic_agent.agent import PydanticAISolverReasoner, ReasoningError
from ctf_agent.pydantic_agent.models import SolverDecision


def _decision():
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


def test_adapter_uses_real_testmodel_and_one_argument_contract():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        adapter = GraphReasonerAdapter(
            PydanticAISolverReasoner.test_model(_decision()),
            challenge={"id": "toy", "title": "Toy", "category": "misc"},
            memory=[{"id": "m", "summary": "memory hint"}],
            skills=[{"note": "skill hint"}],
            tool_capabilities=[{"name": "read_file"}],
            network_authorization_scope={"connection": "local"},
            run_id="run",
            provider_name="test",
            model_name="TestModel",
            iteration_limits={"max_iterations": 2},
            trace_summary=[{"event": "trace"}],
        )
        state = {"iteration": 0, "observations": [{"source": "local"}], "secret": "unit-secret"}
        result = adapter(state)
        assert isinstance(result, SolverDecision)
        deps = adapter.last_dependencies
        assert deps is not None
        assert deps.challenge["id"] == "toy"
        assert deps.evidence_packet["challenge"]["id"] == "toy"
        assert deps.memory_matches and deps.skill_notes and deps.tool_capabilities and deps.network_authorization_scope
        assert "observations" not in deps.graph_state_snapshot
        assert deps.graph_state_snapshot["counts"]["observation_count"] == 1
        assert "unit-secret" not in json.dumps(deps.__dict__)
        assert adapter.reasoner.agent.model.last_model_request_parameters is not None
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_adapter_wraps_reasoning_errors_safely():
    class Broken:
        def reason(self, state, deps):
            raise ReasoningError("bad", "Bearer secret-value")

    adapter = GraphReasonerAdapter(Broken(), challenge={"id": "toy"})
    with pytest.raises(ReasoningError) as exc:
        adapter({"observations": []})
    assert exc.value.code == "graph_reasoning_failed"
    assert "secret-value" not in str(exc.value)
