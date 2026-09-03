from __future__ import annotations

import json

import pytest

from ctf_agent.core.models import Challenge
from ctf_agent.graph.context import build_evidence_packet
from ctf_agent.graph.nodes import summarize_observation, update_hypotheses
from ctf_agent.graph.state import append_evidence_items, initial_workflow_state, restore_workflow_state, serialize_workflow_state


def _state(tmp_path):
    return initial_workflow_state(Challenge(id="state-update", title="State Update", category="web"), run_dir=tmp_path / "run", max_iterations=3)


def _experiment(action_type="read_file"):
    action_input = {"type": action_type}
    if action_type in {"read_file", "inspect_binary"}:
        action_input["path"] = "work/app.php"
    if action_type == "http_request":
        action_input["url"] = "http://challenge.local/"
    return {
        "id": "exp-1",
        "safety_checked": True,
        "action_type": action_type,
        "plan": {
            "goal": "collect evidence",
            "action_type": action_type,
            "action_input": action_input,
            "expected_signal": "expected",
            "failure_signal": "failure",
            "risk": "low",
            "rollback": "none",
        },
    }


def _call(action_type="read_file", **extra):
    call = {
        "id": "call-1",
        "experiment_id": "exp-1",
        "action_type": action_type,
        "action_input": {"type": action_type, "path": "work/app.php"},
        "status": "executed",
        "expected_signal": "expected",
        "failure_signal": "failure",
        "expected_signal_matched": True,
        "failure_signal_matched": False,
        "duration_seconds": 0.01,
        "artifact_paths": ["artifacts/out.txt"],
    }
    call.update(extra)
    return call


def _apply_evidence_update(state, update):
    for key in ("confirmed_facts", "constraints", "anomalies", "evidence_deltas"):
        state[key] = append_evidence_items(state[key], update.get(key, []))


def test_summarize_observation_writes_read_file_delta_and_trace(tmp_path):
    state = _state(tmp_path)
    state["experiments"] = [_experiment("read_file")]
    state["tool_calls"] = [_call("read_file")]
    state["observations"] = [
        {
            "source": "read_file",
            "ok": True,
            "observation_index": 1,
            "evidence": {
                "path": "work/app.php",
                "bytes_read": 42,
                "truncated": False,
                "body_excerpt": "expected text Authorization: Bearer secret-value",
            },
        }
    ]

    update = summarize_observation(state)
    _apply_evidence_update(state, update)

    assert update["phase"] == "summarized"
    assert update["confirmed_facts"]
    assert update["evidence_deltas"]
    facts = json.dumps(state["confirmed_facts"], sort_keys=True)
    assert "read_file_path" in facts
    assert "work/app.php" in facts
    assert "provenance" in facts
    assert "secret-value" not in facts
    assert "Authorization" not in facts
    assert "Bearer" not in facts
    trace = (tmp_path / "run" / "trace.jsonl").read_text(encoding="utf-8")
    assert "evidence-delta" in trace
    assert "secret-value" not in trace


def test_duplicate_facts_are_deduped_and_provenance_is_merged():
    base = {
        "kind": "read_file_path",
        "summary": "read_file succeeded for work/app.php",
        "data": {"path": "work/app.php"},
        "provenance": {"source_type": "read_file", "source_id": "exp-1", "tool_call_id": "call-1", "artifact_path": None, "observation_index": 1},
    }
    duplicate = {
        "kind": "read_file_path",
        "summary": "read_file succeeded for work/app.php",
        "data": {"path": "work/app.php"},
        "provenance": {"source_type": "read_file", "source_id": "exp-2", "tool_call_id": "call-2", "artifact_path": None, "observation_index": 2},
    }

    merged = append_evidence_items([], [base, duplicate])

    assert len(merged) == 1
    assert isinstance(merged[0]["provenance"], list)
    assert {item["source_id"] for item in merged[0]["provenance"]} == {"exp-1", "exp-2"}


def test_blocked_timeout_result_enters_anomalies_and_constraints_not_facts(tmp_path):
    state = _state(tmp_path)
    state["experiments"] = [_experiment("http_request")]
    state["tool_calls"] = [
        _call(
            "http_request",
            status="blocked",
            risk_decision={"level": "refuse", "reason": "blocked outside authorized scope"},
            expected_signal_matched=False,
            failure_signal_matched=True,
        )
    ]
    state["observations"] = [
        {
            "source": "http_request",
            "ok": False,
            "authorization": {"allowed": False, "reason": "blocked outside authorized scope"},
            "evidence": {"exit_code": 124, "timed_out": True},
            "error": "blocked outside authorized scope",
        }
    ]

    update = summarize_observation(state)

    assert update["confirmed_facts"] == []
    constraints = json.dumps(update["constraints"], sort_keys=True)
    anomalies = json.dumps(update["anomalies"], sort_keys=True)
    assert "tool_blocked" in constraints
    assert "failure_signal" in constraints
    assert "tool_failure" in anomalies
    assert "nonzero_exit" in anomalies
    assert "timeout" in anomalies
    assert "expected_signal_missing" in anomalies
    assert "authorization_or_risk_block" in anomalies


def test_php_structured_observation_writes_constraints(tmp_path):
    state = _state(tmp_path)
    state["experiments"] = [_experiment("http_request")]
    state["tool_calls"] = [_call("http_request")]
    state["observations"] = [
        {
            "source": "http_request",
            "ok": True,
            "evidence": {
                "status": 200,
                "php_analysis": {
                    "parameters": [{"superglobal": "GET", "name": "page"}],
                    "sinks": ["include"],
                    "blacklist": ["/flag/"],
                },
            },
        }
    ]

    update = summarize_observation(state)

    constraints = json.dumps(update["constraints"], sort_keys=True)
    assert "php_parameter" in constraints
    assert "php_sink" in constraints
    assert "php_blacklist" in constraints


def test_hypothesis_memory_and_skill_text_do_not_pollute_confirmed_facts(tmp_path):
    state = _state(tmp_path)
    state["experiments"] = [_experiment("read_file")]
    state["hypotheses"] = [{"claim": "model says flag{guess}"}]
    state["memory_matches"] = [{"summary": "memory says flag{memory}"}]
    state["skill_notes"] = [{"summary": "skill says flag{skill}"}]
    state["tool_calls"] = [_call("read_file")]
    state["observations"] = [
        {
            "source": "read_file",
            "ok": True,
            "hypothesis": "flag{hypothesis}",
            "memory": "flag{memory}",
            "skill_notes": ["flag{skill}"],
            "verified_candidates": [{"value": "flag{unverified}", "verified": False}],
            "evidence": {"path": "work/app.php", "bytes_read": 12, "body_excerpt": "ordinary text"},
        }
    ]

    update = summarize_observation(state)

    facts = json.dumps(update["confirmed_facts"], sort_keys=True)
    assert "flag{guess}" not in facts
    assert "flag{memory}" not in facts
    assert "flag{skill}" not in facts
    assert "flag{hypothesis}" not in facts
    assert "flag{unverified}" not in facts


def test_state_roundtrip_and_evidence_packet_read_new_facts(tmp_path):
    state = _state(tmp_path)
    state["experiments"] = [_experiment("read_file")]
    state["tool_calls"] = [_call("read_file")]
    state["observations"] = [
        {"source": "read_file", "ok": True, "evidence": {"path": "work/app.php", "bytes_read": 42, "body_excerpt": "expected"}}
    ]
    update = summarize_observation(state)
    _apply_evidence_update(state, update)

    restored = restore_workflow_state(serialize_workflow_state(state))
    packet = build_evidence_packet(
        restored,
        challenge=restored["challenge"],
        trace_events=[],
        memory=[],
        skills=[],
        tools=[],
        network_scope={},
        limits={},
    )
    encoded = json.dumps(restored, sort_keys=True)

    assert restored["confirmed_facts"] == state["confirmed_facts"]
    assert restored["constraints"] == state["constraints"]
    assert restored["anomalies"] == state["anomalies"]
    assert restored["evidence_deltas"] == state["evidence_deltas"]
    assert json.loads(encoded)["evidence_deltas"]
    assert "read_file_path" in json.dumps(packet.confirmed_facts, default=str)


def test_summarize_and_update_nodes_do_not_call_execution_or_model(tmp_path, monkeypatch):
    state = _state(tmp_path)
    state["experiments"] = [_experiment("read_file")]
    state["tool_calls"] = [_call("read_file")]
    state["observations"] = [{"source": "read_file", "ok": True, "evidence": {"path": "work/app.php"}}]
    import ctf_agent.graph.nodes as nodes

    monkeypatch.setattr(nodes, "read_file", lambda *args, **kwargs: pytest.fail("read_file must not run"))
    monkeypatch.setattr(nodes, "http_request", lambda *args, **kwargs: pytest.fail("http_request must not run"))
    monkeypatch.setattr(nodes, "ask_verifier", lambda *args, **kwargs: pytest.fail("ask_verifier must not run"))

    update = summarize_observation(state)
    hypothesis_update = update_hypotheses(state)

    assert update["evidence_deltas"]
    assert "confirmed_facts" not in hypothesis_update
    assert hypothesis_update["phase"] == "hypotheses-updated"
