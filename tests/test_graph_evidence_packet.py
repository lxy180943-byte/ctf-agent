from __future__ import annotations

import json

from ctf_agent.core.models import Challenge
from ctf_agent.core.trace import TraceEvent
from ctf_agent.graph import EvidencePacket, build_evidence_packet, initial_workflow_state


def _state(tmp_path):
    challenge = Challenge(
        id="evidence-toy",
        title="Evidence Toy",
        category="web",
        description="Inspect the local service.",
        files=["dist/app.py"],
        connection="http://127.0.0.1:8080",
        metadata={"port": 8080, "api_key": "challenge-secret"},
    )
    state = initial_workflow_state(challenge, run_dir=tmp_path / "run", max_iterations=5)
    state["confirmed_facts"] = [
        {"source": "observation:headers", "summary": "Server returns Flask header", "status": 200}
    ]
    state["hypotheses"] = [
        {
            "claim": "The flag is probably flag{llm_guess}",
            "confidence": 0.8,
            "evidence_for": ["model intuition only"],
        }
    ]
    long_body = "A" * 1600 + "\nAuthorization: Bearer super-secret-token-value"
    state["observations"] = [
        {"source": f"http-{index}", "summary": f"HTTP response {index}", "raw": long_body}
        for index in range(14)
    ]
    state["observations"].append(
        {"source": "verifier", "summary": "Verifier checked candidate", "verified": True}
    )
    state["experiments"] = [
        {"source": "experiment", "goal": f"try path {index}", "result_summary": f"status {index}"}
        for index in range(10)
    ]
    state["artifacts"] = [
        {"source": "artifact", "path": str(tmp_path / "run" / "out.txt"), "summary": "Captured output"}
    ]
    state["failed_actions"] = [
        {"source": "tool", "kind": "failed_action", "summary": "Command rejected by policy"}
    ]
    state["verified_candidates"] = [
        {"value": "flag{verified}", "source": "verifier", "verified": True},
        {"value": "flag{unverified}", "source": "model", "verified": False},
    ]
    return challenge, state


def _packet(tmp_path, *, limits=None) -> EvidencePacket:
    challenge, state = _state(tmp_path)
    return build_evidence_packet(
        state,
        challenge=challenge,
        trace_events=[
            TraceEvent(
                challenge_id=challenge.id,
                agent="executor",
                action="run",
                stderr="token=trace-secret",
                exit_code=1,
                timestamp="2026-01-01T00:00:00Z",
                id="trace-1",
            )
        ],
        memory=[
            {"source_run": "run-1", "confidence": 0.7, "summary": "Try /debug", "token": "memory-secret"}
            for _ in range(7)
        ],
        skills=[
            {"source": "ctf-web", "summary": "Check templates", "Authorization": "Bearer skill-secret"}
            for _ in range(9)
        ],
        tools=[{"name": "read_file", "description": "Read authorized files"}],
        network_scope={"allowed_hosts": ["127.0.0.1"], "Authorization": "Bearer net-secret"},
        limits=limits,
    )


def test_evidence_packet_enforces_sources_and_advisory_flags(tmp_path):
    packet = _packet(tmp_path)

    confirmed = json.dumps([fact.model_dump(mode="json") for fact in packet.confirmed_facts])
    assert "Server returns Flask header" in confirmed
    assert "flag{llm_guess}" not in confirmed
    assert all(note.advisory for note in packet.memory_notes)
    assert all(note.advisory for note in packet.skill_notes)
    assert all(hypothesis.advisory for hypothesis in packet.active_hypotheses)


def test_evidence_packet_redacts_truncates_and_filters_candidates(tmp_path):
    packet = _packet(tmp_path)
    payload = json.dumps(packet.model_dump(mode="json"), sort_keys=True)

    assert "super-secret-token-value" not in payload
    assert "memory-secret" not in payload
    assert "skill-secret" not in payload
    assert "net-secret" not in payload
    assert "challenge-secret" not in payload
    assert "<redacted>" in payload
    assert "... <truncated " in payload
    assert packet.verified_candidates == [
        {"value": "flag{verified}", "source": "verifier", "verified": True}
    ]
    assert "flag{unverified}" not in json.dumps(packet.verified_candidates)


def test_evidence_packet_default_budgets_are_applied(tmp_path):
    packet = _packet(tmp_path)

    assert len(packet.recent_observations) == 12
    assert len(packet.recent_experiments) == 8
    assert len(packet.memory_notes) == 5
    assert len(packet.skill_notes) == 8
    assert packet.context_budget["sections"]["observations"]["original_count"] == 15
    assert packet.context_budget["sections"]["observations"]["omitted_count"] == 3
    assert packet.context_budget["sections"]["memory"]["omitted_count"] == 2
    assert packet.context_budget["sections"]["skills"]["omitted_count"] == 1


def test_evidence_packet_custom_limits_json_and_stability(tmp_path):
    first = _packet(tmp_path, limits={"observations": 2, "experiments": 1, "memory": 1, "skills": 1, "summary_chars": 200})
    second = _packet(tmp_path, limits={"observations": 2, "experiments": 1, "memory": 1, "skills": 1, "summary_chars": 200})

    assert len(first.recent_observations) == 2
    assert len(first.recent_experiments) == 1
    encoded = json.dumps(first.model_dump(mode="json"), sort_keys=True)
    assert json.loads(encoded)["challenge"]["id"] == "evidence-toy"
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
