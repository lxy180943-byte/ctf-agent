from __future__ import annotations

import json
from pathlib import Path

from ctf_agent.core.models import Challenge
from ctf_agent.graph.state import (
    STATE_FIELD_POLICIES,
    apply_model_update,
    append_items,
    merge_experiment_items,
    initial_workflow_state,
    restore_workflow_state,
    serialize_workflow_state,
    workflow_state_to_json,
)


def test_initial_workflow_state_is_complete_and_json_safe(tmp_path):
    challenge = Challenge(id="graph-toy", title="Graph Toy", category="web")
    state = initial_workflow_state(challenge, run_dir=tmp_path / "run", max_iterations=7)

    assert set(STATE_FIELD_POLICIES).issubset(state)
    assert state["challenge"]["id"] == "graph-toy"
    assert state["run_dir"] == str(tmp_path / "run")
    assert state["max_iterations"] == 7
    assert state["solved"] is False
    assert state["verified_candidates"] == []
    assert json.loads(workflow_state_to_json(state))["phase"] == "initialize"


def test_append_reducer_keeps_observations_events_and_tool_calls():
    existing = [{"source": "first"}]
    assert append_items(existing, {"source": "second"}) == [
        {"source": "first"},
        {"source": "second"},
    ]
    assert append_items([], [{"event": "reason"}, {"tool": "read_file"}]) == [
        {"event": "reason"},
        {"tool": "read_file"},
    ]


def test_experiment_reducer_merges_completion_update_by_id():
    planned = {
        "id": "exp-1",
        "action_type": "read_file",
        "plan": {"goal": "read a", "action_input": {"path": "a.txt"}},
        "completed": False,
        "status": "planned",
    }
    completed = {
        "id": "exp-1",
        "completed": True,
        "status": "completed",
        "outcome": "executed",
        "tool_call_id": "tool-call-exp-1",
    }

    merged = merge_experiment_items([planned], [completed])

    assert len(merged) == 1
    assert merged[0]["plan"] == planned["plan"]
    assert merged[0]["completed"] is True
    assert merged[0]["status"] == "completed"
    assert merged[0]["outcome"] == "executed"
    assert merged[0]["tool_call_id"] == "tool-call-exp-1"


def test_experiment_reducer_appends_distinct_experiment_ids():
    merged = merge_experiment_items(
        [{"id": "exp-1", "action_type": "read_file"}],
        [{"id": "exp-2", "action_type": "read_file"}],
    )

    assert [item["id"] for item in merged] == ["exp-1", "exp-2"]


def test_experiment_reducer_handles_duplicate_completion_and_no_aliasing():
    planned = {
        "id": "exp-1",
        "action_type": "read_file",
        "plan": {"goal": "read", "action_input": {"path": "a.txt"}},
        "completed": False,
        "status": "planned",
    }
    first_completion = {"id": "exp-1", "completed": True, "status": "completed", "outcome": "executed"}
    second_completion = {"id": "exp-1", "completed": True, "status": "completed", "outcome": "executed", "tool_call_id": "call-1"}

    merged = merge_experiment_items([planned], [first_completion, second_completion])
    planned["plan"]["goal"] = "mutated after merge"
    second_completion["tool_call_id"] = "mutated-call"

    assert len(merged) == 1
    assert merged[0]["plan"]["goal"] == "read"
    assert merged[0]["completed"] is True
    assert merged[0]["tool_call_id"] == "call-1"


def test_experiment_reducer_merges_after_json_roundtrip(tmp_path):
    state = initial_workflow_state({"id": "roundtrip", "title": "Roundtrip", "category": "misc"}, run_dir=tmp_path / "run")
    state["experiments"] = merge_experiment_items(
        state["experiments"],
        {"id": "exp-1", "action_type": "read_file", "plan": {"goal": "read"}, "completed": False, "status": "planned"},
    )
    restored = restore_workflow_state(serialize_workflow_state(state))
    restored["experiments"] = merge_experiment_items(
        restored["experiments"],
        {"id": "exp-1", "completed": True, "status": "completed", "outcome": "blocked"},
    )

    assert len(restored["experiments"]) == 1
    assert restored["experiments"][0]["plan"] == {"goal": "read"}
    assert restored["experiments"][0]["outcome"] == "blocked"


def test_workflow_state_round_trip_supports_resume(tmp_path):
    state = initial_workflow_state(
        {"id": "resume-toy", "title": "Resume", "category": "misc"},
        run_dir=Path(tmp_path) / "run",
        memory_matches=[{"id": "memory-1"}],
        skill_notes=[{"path": "SKILL.md"}],
    )
    state["observations"] = append_items(state["observations"], {"status": 200})
    state["events"] = append_items(state["events"], {"action": "reason"})
    state["tool_calls"] = append_items(state["tool_calls"], {"tool": "read_file"})
    state["iteration"] = 3
    state["paused"] = True

    restored = restore_workflow_state(serialize_workflow_state(state))
    assert restored["observations"] == [{"status": 200}]
    assert restored["events"] == [{"action": "reason"}]
    assert restored["tool_calls"] == [{"tool": "read_file"}]
    assert restored["memory_matches"] == [{"id": "memory-1"}]
    assert restored["skill_notes"] == [{"path": "SKILL.md"}]
    assert restored["iteration"] == 3
    assert restored["paused"] is True


def test_model_updates_reject_runtime_owned_state(tmp_path):
    state = initial_workflow_state(
        {"id": "guard-toy", "title": "Guard", "category": "misc"},
        run_dir=tmp_path / "run",
    )
    updated = apply_model_update(state, {"next_goal": "inspect source", "unknowns": ["entry point"]})
    assert updated["next_goal"] == "inspect source"
    assert updated["unknowns"] == ["entry point"]

    for field in ("solved", "verified_candidates", "artifacts"):
        try:
            apply_model_update(state, {field: True})
        except PermissionError:
            continue
        raise AssertionError(f"{field} must remain runtime-owned")

