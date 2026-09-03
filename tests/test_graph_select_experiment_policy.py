from __future__ import annotations

import json

from ctf_agent.core.models import Challenge
from ctf_agent.graph.edges import after_select_experiment
from ctf_agent.graph.nodes import execute_experiment, select_experiment
from ctf_agent.graph.state import append_items, restore_workflow_state, serialize_workflow_state, initial_workflow_state


def _read(path: str = "input/app.php") -> dict:
    return {
        "goal": "Read source file",
        "action_type": "read_file",
        "action_input": {"type": "read_file", "path": path},
        "expected_signal": "php source",
        "failure_signal": "file missing",
        "risk": "low",
        "rollback": "none",
    }


def _http(url: str = "http://evil.example/index.php?id=1") -> dict:
    return {
        "goal": "Probe HTTP parameter",
        "action_type": "http_request",
        "action_input": {"type": "http_request", "method": "GET", "url": url, "params": {}, "headers": {"Authorization": "Bearer secret-value"}, "body": None, "timeout": 20},
        "expected_signal": "status",
        "failure_signal": "403",
        "risk": "low",
        "rollback": "none",
    }


def _state(tmp_path, selected: dict | None = None):
    state = initial_workflow_state(Challenge(id="select-policy", title="Select policy", category="web"), run_dir=tmp_path / "run")
    state["iteration"] = 1
    state["unknowns"] = ["entry point"]
    if selected is not None:
        state["events"].append({"kind": "reasoning-decision", "decision": {"selected_experiment": selected}})
    return state


def _merge(state, update):
    merged = dict(state)
    for key, value in update.items():
        if key in {"experiment_assessments", "observations", "events", "experiments", "failed_actions", "tool_calls"}:
            merged[key] = append_items(merged.get(key, []), value)
        else:
            merged[key] = value
    return merged


def _record_attempt(state, plan, *, experiment_id="exp-1", status="executed", failure=False):
    state["experiments"].append({"id": experiment_id, "action_type": plan["action_type"], "plan": plan, "completed": True})
    state["tool_calls"].append({"id": f"call-{experiment_id}", "experiment_id": experiment_id, "action_type": plan["action_type"], "action_input": plan["action_input"], "status": status, "failure_signal_matched": failure})


def test_high_information_gain_unread_file_proceeds(tmp_path):
    state = _state(tmp_path, _read())

    update = select_experiment(state)
    routed = after_select_experiment(_merge(state, update))

    assert update["phase"] == "experiment-selected"
    assert update["replan_required"] is False
    assert update["experiments"][0]["safety_checked"] is True
    assert update["last_experiment_assessment"]["recommended_action"] == "proceed"
    assert routed == "execute_experiment"


def test_duplicate_without_new_evidence_replans_and_does_not_execute(tmp_path, monkeypatch):
    plan = _read()
    state = _state(tmp_path, plan)
    _record_attempt(state, plan)
    import ctf_agent.graph.nodes as nodes

    monkeypatch.setattr(nodes, "read_file", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("read_file must not run")))

    update = select_experiment(state)
    routed = after_select_experiment(_merge(state, update))
    noop = execute_experiment(_merge(state, update))

    assert update["phase"] == "replan-required"
    assert update["replan_required"] is True
    assert "experiments" not in update
    assert routed == "reason_about_challenge"
    assert noop["phase"] == "replan-required"
    assert update["observations"][0]["duplicate"] is True


def test_known_network_risk_or_path_conflict_pauses_or_replans_without_execution(tmp_path, monkeypatch):
    state = _state(tmp_path, _http())
    state["constraints"].append({"kind": "network_scope", "summary": "authorized host", "data": {"allowed_hosts": ["web.local"]}})
    import ctf_agent.graph.nodes as nodes

    monkeypatch.setattr(nodes, "http_request", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("http_request must not run")))

    update = select_experiment(state)
    routed = after_select_experiment(_merge(state, update))
    noop = execute_experiment(_merge(state, update))

    assert update["last_experiment_assessment"]["blocked_by_constraint"] is True
    assert update["phase"] in {"paused", "replan-required"}
    assert "experiments" not in update
    assert routed in {"human_review", "reason_about_challenge"}
    assert noop["phase"] in {"paused", "replan-required"}
    assert "secret-value" not in json.dumps(update)


def test_two_consecutive_failed_same_fingerprint_replans(tmp_path):
    plan = _read()
    state = _state(tmp_path, plan)
    _record_attempt(state, plan, experiment_id="exp-1", status="failed", failure=True)
    _record_attempt(state, plan, experiment_id="exp-2", status="failed", failure=True)

    update = select_experiment(state)

    assert update["replan_required"] is True
    assert update["last_experiment_assessment"]["recommended_action"] == "replan"
    assert any("failed twice" in reason for reason in update["observations"][0]["reasons"])


def test_replan_observation_contains_reasoner_usable_reasons(tmp_path):
    plan = _read()
    state = _state(tmp_path, plan)
    _record_attempt(state, plan)

    update = select_experiment(state)
    observation = update["observations"][0]

    assert observation["source"] == "experiment_policy"
    assert observation["kind"] == "experiment_assessment"
    assert observation["fingerprint_digest"]
    assert observation["reasons"]
    assert observation["recommended_action"] == "replan"


def test_three_consecutive_replans_pause_without_infinite_loop(tmp_path):
    plan = _read()
    state = _state(tmp_path, plan)
    state["consecutive_replans"] = 2
    _record_attempt(state, plan)

    update = select_experiment(state)
    routed = after_select_experiment(_merge(state, update))

    assert update["phase"] == "paused"
    assert update["paused"] is True
    assert update["consecutive_replans"] == 3
    assert "different direction" in update["pending_human_question"]
    assert routed == "human_review"


def test_assessment_state_is_json_and_checkpoint_roundtrip_safe(tmp_path):
    state = _state(tmp_path, _read())
    update = select_experiment(state)
    merged = _merge(state, update)
    restored = restore_workflow_state(serialize_workflow_state(merged))

    assert restored["last_experiment_assessment"]["fingerprint"]["digest"]
    assert restored["experiment_assessments"]
    assert json.dumps(restored, sort_keys=True)


def test_trace_has_experiment_assessed_and_no_tool_or_model_call(tmp_path, monkeypatch):
    state = _state(tmp_path, _read())
    import ctf_agent.graph.nodes as nodes

    monkeypatch.setattr(nodes, "read_file", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("read_file must not run")))
    monkeypatch.setattr(nodes, "http_request", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("http_request must not run")))
    monkeypatch.setattr(nodes, "ask_verifier", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ask_verifier must not run")))

    update = select_experiment(state)
    trace = (tmp_path / "run" / "trace.jsonl").read_text(encoding="utf-8")

    assert update["last_experiment_assessment"]["recommended_action"] == "proceed"
    assert "experiment-assessed" in trace
    assert "secret" not in trace
