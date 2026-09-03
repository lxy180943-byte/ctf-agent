from __future__ import annotations

import json

import pytest

from ctf_agent.core.models import Challenge
from ctf_agent.graph.nodes import update_hypotheses
from ctf_agent.graph.state import append_items, initial_workflow_state, restore_workflow_state, serialize_workflow_state


def _state(tmp_path):
    state = initial_workflow_state(Challenge(id="reconcile", title="Reconcile", category="web"), run_dir=tmp_path / "run", max_iterations=5)
    state["iteration"] = 2
    state["current_hypothesis"] = "main"
    state["hypotheses"] = [
        {
            "id": "main",
            "name": "main",
            "claim": "The source disclosure path exposes PHP logic.",
            "confidence": 0.50,
            "status": "active",
            "evidence_for": [],
            "evidence_against": [],
            "falsification_test": "failure_signal",
        },
        {
            "id": "backup",
            "name": "backup",
            "claim": "Search artifacts for static hints.",
            "confidence": 0.40,
            "status": "active",
            "evidence_for": [],
            "evidence_against": [],
            "falsification_test": "no artifact matches",
        },
    ]
    state["candidate_chains"] = [
        {"id": "main", "name": "main", "steps": ["read source", "test include"], "confidence": 0.50, "status": "active", "evidence_for": [], "evidence_against": []},
        {"id": "backup", "name": "backup", "steps": ["search artifacts"], "confidence": 0.40, "status": "active", "evidence_for": [], "evidence_against": []},
    ]
    state["experiments"] = [{"id": "exp-1", "action_type": "read_file", "plan": {"failure_signal": "failure_signal"}}]
    state["tool_calls"] = [
        {
            "id": "call-1",
            "experiment_id": "exp-1",
            "action_type": "read_file",
            "status": "executed",
            "expected_signal": "expected",
            "failure_signal": "failure_signal",
            "expected_signal_matched": False,
            "failure_signal_matched": False,
        }
    ]
    return state


def _delta(*, confirmed=None, constraints=None, anomalies=None):
    return {
        "confirmed_facts": confirmed or [],
        "constraints": constraints or [],
        "anomalies": anomalies or [],
        "candidate_artifacts": [],
        "provenance": {"source_type": "read_file", "source_id": "exp-1", "tool_call_id": "call-1", "artifact_path": "artifacts/out.txt", "observation_index": 1},
        "extraction_notes": [],
    }


def _apply_update(state, update):
    state["hypotheses"] = update["hypotheses"]
    state["candidate_chains"] = update["candidate_chains"]
    state["current_hypothesis"] = update["current_hypothesis"]
    state["hypothesis_updates"] = append_items(state["hypothesis_updates"], update.get("hypothesis_updates", []))


def test_expected_signal_and_confirmed_fact_raise_confidence(tmp_path):
    state = _state(tmp_path)
    state["tool_calls"][-1]["expected_signal_matched"] = True
    state["evidence_deltas"] = [
        _delta(confirmed=[{"kind": "read_file_path", "summary": "expected PHP source was read", "provenance": {"source_id": "exp-1"}}])
    ]

    update = update_hypotheses(state)

    main = update["hypotheses"][0]
    chain = update["candidate_chains"][0]
    assert main["confidence"] == pytest.approx(0.65)
    assert main["status"] == "active"
    assert "expected PHP source was read" in json.dumps(main["evidence_for"])
    assert chain["confidence"] == pytest.approx(0.65)
    assert update["current_hypothesis"] == "main"


def test_failure_signal_lowers_confidence(tmp_path):
    state = _state(tmp_path)
    state["tool_calls"][-1]["failure_signal_matched"] = True
    state["evidence_deltas"] = [_delta(anomalies=[{"kind": "failure_signal_matched", "summary": "failure_signal was observed"}])]

    update = update_hypotheses(state)

    main = update["hypotheses"][0]
    assert main["confidence"] <= 0.35
    assert main["status"] in {"weakened", "falsified"}
    assert main["failed_experiment_count"] == 1
    assert "failure_signal" in main["update_reason"] or "falsification" in main["update_reason"]


def test_falsification_test_match_marks_falsified(tmp_path):
    state = _state(tmp_path)
    state["tool_calls"][-1]["failure_signal_matched"] = True
    state["evidence_deltas"] = [_delta(anomalies=[{"kind": "failure_signal_matched", "summary": "failure_signal matched observed output"}])]

    update = update_hypotheses(state)

    main = update["hypotheses"][0]
    assert main["status"] == "falsified"
    assert main["confidence"] <= 0.10
    assert update["current_hypothesis"] == "backup"


def test_two_same_failures淘汰_current_chain_and_switches_to_active_chain(tmp_path):
    state = _state(tmp_path)
    state["hypotheses"][0]["falsification_test"] = "specific source marker absent"
    state["hypotheses"][0]["failed_experiment_count"] = 1
    state["hypotheses"][0]["last_failure_kind"] = "failure_signal"
    state["candidate_chains"][0]["failed_experiment_count"] = 1
    state["candidate_chains"][0]["last_failure_kind"] = "failure_signal"
    state["tool_calls"][-1]["failure_signal_matched"] = True
    state["evidence_deltas"] = [_delta(anomalies=[{"kind": "failure_signal_matched", "summary": "same failure signal again"}])]

    update = update_hypotheses(state)

    assert update["hypotheses"][0]["status"] == "falsified"
    assert update["candidate_chains"][0]["status"] == "falsified"
    assert update["current_hypothesis"] == "backup"


def test_tool_missing_or_authorization_block_does_not_falsify_vulnerability_chain(tmp_path):
    state = _state(tmp_path)
    state["hypotheses"][0]["falsification_test"] = "source content disproves include path"
    state["tool_calls"][-1].update({"status": "blocked", "expected_signal_matched": False})
    state["evidence_deltas"] = [
        _delta(
            constraints=[{"kind": "tool_blocked", "summary": "tool execution was blocked"}],
            anomalies=[{"kind": "tool_failure", "summary": "tool unavailable because network authorization not enabled"}],
        )
    ]

    update = update_hypotheses(state)

    main = update["hypotheses"][0]
    assert main["status"] == "weakened"
    assert main["confidence"] == pytest.approx(0.35)
    assert "externally" in main["update_reason"]


def test_confidence_is_clamped_and_memory_skill_do_not_raise_confidence(tmp_path):
    state = _state(tmp_path)
    state["hypotheses"][0]["confidence"] = 0.95
    state["candidate_chains"][0]["confidence"] = 0.95
    state["memory_matches"] = [{"summary": "memory says this is likely"}]
    state["skill_notes"] = [{"summary": "skill says common exploit"}]
    state["tool_calls"][-1]["expected_signal_matched"] = True
    state["evidence_deltas"] = [_delta(confirmed=[{"kind": "read_file_path", "summary": "expected source observed"}])]

    update = update_hypotheses(state)

    assert update["hypotheses"][0]["confidence"] == 1.0
    state = _state(tmp_path)
    state["hypotheses"][0]["confidence"] = 0.05
    state["tool_calls"][-1]["failure_signal_matched"] = True
    state["evidence_deltas"] = [_delta(anomalies=[{"kind": "failure_signal_matched", "summary": "failure_signal"}])]
    update = update_hypotheses(state)
    assert 0.0 <= update["hypotheses"][0]["confidence"] <= 0.10


def test_reconciliation_audit_trace_and_roundtrip_are_safe(tmp_path):
    state = _state(tmp_path)
    state["tool_calls"][-1]["expected_signal_matched"] = True
    state["evidence_deltas"] = [_delta(confirmed=[{"kind": "read_file_path", "summary": "expected source observed Authorization: Bearer secret-value"}])]

    update = update_hypotheses(state)
    _apply_update(state, update)
    restored = restore_workflow_state(serialize_workflow_state(state))
    trace = (tmp_path / "run" / "trace.jsonl").read_text(encoding="utf-8")

    assert restored["hypotheses"][0]["last_updated_iteration"] == 2
    assert restored["hypothesis_updates"]
    assert "hypothesis-reconciled" in trace
    assert "secret-value" not in trace
    assert "Authorization" not in trace
    assert json.dumps(restored, sort_keys=True)


def test_update_hypotheses_does_not_call_model_executor_or_network(tmp_path, monkeypatch):
    state = _state(tmp_path)
    state["evidence_deltas"] = [_delta(confirmed=[{"kind": "read_file_path", "summary": "expected source observed"}])]
    import ctf_agent.graph.nodes as nodes

    monkeypatch.setattr(nodes, "read_file", lambda *args, **kwargs: pytest.fail("read_file must not run"))
    monkeypatch.setattr(nodes, "http_request", lambda *args, **kwargs: pytest.fail("http_request must not run"))
    monkeypatch.setattr(nodes, "ask_verifier", lambda *args, **kwargs: pytest.fail("ask_verifier must not run"))

    update = update_hypotheses(state)

    assert update["hypotheses"]
    assert "solved" not in update
    assert "verified_candidates" not in update
