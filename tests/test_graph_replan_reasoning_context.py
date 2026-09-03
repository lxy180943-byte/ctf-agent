from __future__ import annotations

import asyncio
import json

from ctf_agent.core.models import Challenge
from ctf_agent.graph.context import build_evidence_packet
from ctf_agent.graph.reasoner_adapter import GraphReasonerAdapter
from ctf_agent.graph.state import initial_workflow_state
from ctf_agent.pydantic_agent.agent import PydanticAISolverReasoner, SolverDependencies, SYSTEM_PROMPT
from ctf_agent.pydantic_agent.models import SolverDecision


def _decision():
    return {
        "current_hypothesis": {
            "name": "different experiment",
            "claim": "Use replan history to choose a materially different local file read.",
            "evidence_for": ["replan history is present"],
            "evidence_against": [],
            "confidence": 0.45,
            "falsification_test": "new file read also lacks source evidence",
        },
        "confirmed_facts": ["Only restate packet facts."],
        "unknowns": ["which source file contains routing"],
        "candidate_chains": [["read a different file", "compare routing evidence"]],
        "selected_experiment": {
            "goal": "Read a different route source file.",
            "action_type": "read_file",
            "action_input": {"type": "read_file", "path": "src/routes.php"},
            "expected_signal": "route definitions",
            "failure_signal": "file missing",
            "risk": "low",
            "rollback": "no mutation",
        },
        "next_action": "execute_selected_experiment",
        "need_human": False,
        "stop_reason": None,
    }


def _assessment(iteration: int, *, digest: str, action_type: str = "http_request", reason: str = "duplicate request with Authorization: Bearer secret-token") -> dict:
    return {
        "fingerprint": {
            "action_type": action_type,
            "digest": digest,
            "normalized_input": {
                "header_names": ["authorization", "x-test"],
                "command_summary": "curl http://target/?api_key=secret-token",
            },
        },
        "action_type": action_type,
        "recommended_action": "replan",
        "duplicate": True,
        "blocked_by_constraint": False,
        "information_gain_score": 0.1,
        "risk_penalty": 0.25,
        "reasons": [reason],
        "missing_question": "Which file should answer the route unknown?",
        "iteration": iteration,
    }


def _state(tmp_path):
    challenge = Challenge(id="replan-context", title="Replan Context", category="web", files=["src/index.php"])
    state = initial_workflow_state(challenge, run_dir=tmp_path / "run", max_iterations=6)
    state["unknowns"] = ["route source file", "parameter blacklist"]
    for index in range(7):
        state["experiment_assessments"].append(_assessment(index, digest=f"digest-{index}"))
    state["observations"].append(
        {
            "source": "experiment_policy",
            "kind": "experiment_assessment",
            "recommended_action": "replan",
            "fingerprint_digest": "digest-observation",
            "action_type": "read_file",
            "reasons": ["missing source path still unanswered"],
            "missing_question": "Which file defines routes?",
            "iteration": 7,
        }
    )
    return challenge, state


def test_evidence_packet_contains_bounded_replan_context(tmp_path):
    challenge, state = _state(tmp_path)

    packet = build_evidence_packet(
        state,
        challenge=challenge.to_dict(),
        trace_events=[],
        memory=[],
        skills=[],
        tools=[{"name": "read_file"}],
        network_scope={},
        limits={"summary_chars": 200},
    )
    data = packet.model_dump(mode="json")

    assert len(data["recent_experiment_assessments"]) == 5
    assert len(data["replan_history"]) == 3
    assert data["recent_experiment_assessments"][-1]["fingerprint"] == {"action_type": "http_request", "digest": "digest-6"}
    assert data["prohibited_fingerprints"][-1]["digest"] == "digest-6"
    assert "Which file should answer" in json.dumps(data["unanswered_questions"])
    assert "route source file" in json.dumps(data["unanswered_questions"])
    assert data["context_budget"]["sections"]["experiment_assessments"]["omitted_count"] == 2
    serialized = json.dumps(data, sort_keys=True)
    assert "secret-token" not in serialized
    assert "api_key" not in serialized
    assert "Bearer" not in serialized
    assert "Authorization" not in serialized
    assert "command_summary" not in serialized
    assert "header_names" not in serialized


def test_prompt_contains_replan_avoidance_rules():
    prompt = SYSTEM_PROMPT.lower()

    assert "replan_history" in prompt
    assert "previous experiment was invalid or rejected" in prompt
    assert "must not repeat any evidencepacket.prohibited_fingerprints" in prompt
    assert "unanswered_questions" in prompt
    assert "every safe experiment is blocked" in prompt
    assert "meaningless parameters" in prompt
    assert "differ materially in goal, path, target file, unknown condition, or tested precondition" in prompt


def test_replan_context_reaches_testmodel_through_adapter(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        challenge, state = _state(tmp_path)
        reasoner = PydanticAISolverReasoner.test_model(_decision())
        adapter = GraphReasonerAdapter(
            reasoner,
            challenge=challenge.to_dict(),
            memory=[],
            skills=[],
            tool_capabilities=[{"name": "read_file"}],
            network_authorization_scope={},
            run_id="run-replan",
            provider_name="test",
            model_name="TestModel",
            iteration_limits={"max_iterations": 6, "summary_chars": 220, "experiment_assessments": 5, "replans": 3},
            trace_summary=[],
        )

        result = adapter(state)

        assert isinstance(result, SolverDecision)
        deps = adapter.last_dependencies
        assert isinstance(deps, SolverDependencies)
        packet = deps.evidence_packet
        assert packet["replan_history"]
        assert packet["prohibited_fingerprints"]
        assert packet["unanswered_questions"]
        assert len(packet["recent_experiment_assessments"]) == 5
        assert len(packet["replan_history"]) == 3
        assert reasoner.agent.model.last_model_request_parameters is not None
        serialized = json.dumps(deps.__dict__, sort_keys=True)
        for forbidden in ("secret-token", "Authorization", "Bearer", "api_key", "command_summary"):
            assert forbidden not in serialized
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_replan_reasoning_context_does_not_call_tools(tmp_path, monkeypatch):
    challenge, state = _state(tmp_path)
    import ctf_agent.graph.reasoner_adapter as adapter_module

    monkeypatch.setattr(adapter_module, "build_evidence_packet", adapter_module.build_evidence_packet)
    reasoner = PydanticAISolverReasoner.test_model(_decision())
    adapter = GraphReasonerAdapter(reasoner, challenge=challenge.to_dict(), tool_capabilities=[])

    result = adapter(state)

    assert isinstance(result, SolverDecision)
