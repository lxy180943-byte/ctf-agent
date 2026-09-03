from __future__ import annotations

import asyncio
import json

from ctf_agent.core.models import Challenge
from ctf_agent.graph.reasoner_adapter import GraphReasonerAdapter
from ctf_agent.graph.state import initial_workflow_state
from ctf_agent.pydantic_agent.agent import PydanticAISolverReasoner, SolverDependencies
from ctf_agent.pydantic_agent.models import SolverDecision


def _decision():
    return {
        "current_hypothesis": {
            "name": "inspect",
            "claim": "Inspect bounded evidence before choosing the next local file.",
            "evidence_for": ["packet contains known paths"],
            "evidence_against": [],
            "confidence": 0.4,
            "falsification_test": "Read the named file and compare output.",
        },
        "confirmed_facts": ["The challenge is local and authorized."],
        "unknowns": ["file contents"],
        "candidate_chains": [["inspect file", "summarize evidence"]],
        "selected_experiment": {
            "goal": "Read the candidate source file.",
            "action_type": "read_file",
            "action_input": {"type": "read_file", "path": "dist/app.py"},
            "expected_signal": "source text",
            "failure_signal": "read failure",
            "risk": "low",
            "rollback": "no mutation",
        },
        "next_action": "execute_selected_experiment",
        "need_human": False,
        "stop_reason": None,
    }


def _state(tmp_path):
    challenge = Challenge(
        id="adapter-evidence",
        title="Adapter Evidence",
        category="web",
        description="Use evidence only.",
        files=["dist/app.py"],
        connection="http://127.0.0.1:8080",
        metadata={"api_key": "challenge-secret", "difficulty": "toy"},
    )
    state = initial_workflow_state(challenge, run_dir=tmp_path / "run", max_iterations=4)
    state["phase"] = "reasoning"
    state["iteration"] = 2
    state["confirmed_facts"] = [{"source": "observation", "summary": "Port 8080 returns HTTP 200"}]
    state["hypotheses"] = [{"claim": "Maybe there is an admin page with model invented text", "confidence": 0.6}]
    state["current_hypothesis"] = "Check source routes"
    long_text = "B" * 5000 + "\nAuthorization: Bearer graph-state-secret"
    state["observations"] = [{"source": "http", "summary": "Large response", "raw": long_text} for _ in range(13)]
    state["experiments"] = [{"goal": "probe", "result_summary": "large result " + long_text}]
    state["verified_candidates"] = [
        {"value": "verified-candidate", "source": "verifier", "verified": True},
        {"value": "unverified-candidate", "source": "model", "verified": False},
    ]
    state["secret"] = "raw-state-secret"
    return challenge, state


def test_adapter_passes_evidence_packet_to_real_testmodel(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        challenge, state = _state(tmp_path)
        reasoner = PydanticAISolverReasoner.test_model(_decision())
        adapter = GraphReasonerAdapter(
            reasoner,
            challenge=challenge.to_dict(),
            memory=[{"source_run": "run-1", "summary": "Memory note", "confidence": 0.7, "token": "memory-secret"}],
            skills=[{"source": "ctf-web", "summary": "Skill note", "Authorization": "Bearer skill-secret"}],
            tool_capabilities=[{"name": "read_file", "description": "Read local files"}],
            network_authorization_scope={"allowed_hosts": ["127.0.0.1"], "Authorization": "Bearer net-secret"},
            run_id="run-1",
            provider_name="test",
            model_name="TestModel",
            iteration_limits={"max_iterations": 4, "summary_chars": 300},
            trace_summary=[{"source": "trace", "stderr": "Authorization: Bearer trace-secret", "exit_code": 1}],
        )
        result = adapter(state)
        assert isinstance(result, SolverDecision)
        assert reasoner.agent.model.last_model_request_parameters is not None

        deps = adapter.last_dependencies
        assert isinstance(deps, SolverDependencies)
        packet = deps.evidence_packet
        assert packet["challenge"]["id"] == "adapter-evidence"
        assert packet["tool_capabilities"] == [{"name": "read_file", "description": "Read local files"}]
        assert packet["network_authorization_scope"]["allowed_hosts"] == ["127.0.0.1"]
        assert packet["known_paths"]
        assert packet["verified_candidates"] == [{"value": "verified-candidate", "source": "verifier", "verified": True}]
        assert deps.graph_state_snapshot["iteration"] == 2
        assert deps.graph_state_snapshot["max_iterations"] == 4
        assert deps.graph_state_snapshot["current_hypothesis"] == "Check source routes"

        confirmed = json.dumps(packet["confirmed_facts"], sort_keys=True)
        assert "Port 8080 returns HTTP 200" in confirmed
        assert "model invented text" not in confirmed
        assert "Memory note" not in confirmed
        assert "Skill note" not in confirmed
        assert all(item["advisory"] for item in packet["memory_notes"])
        assert all(item["advisory"] for item in packet["skill_notes"])

        serialized = json.dumps(deps.__dict__, sort_keys=True)
        for forbidden in (
            "raw-state-secret",
            "graph-state-secret",
            "memory-secret",
            "skill-secret",
            "net-secret",
            "trace-secret",
            "challenge-secret",
            "Authorization",
            "Bearer",
            "api_key",
        ):
            assert forbidden not in serialized
        assert "B" * 2000 not in serialized
        json.dumps(packet, sort_keys=True)
    finally:
        loop.close()
        asyncio.set_event_loop(None)
