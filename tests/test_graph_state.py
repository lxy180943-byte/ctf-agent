from __future__ import annotations

import json
from pathlib import Path

from ctf_agent.core.models import Challenge
from ctf_agent.graph.state import (
    STATE_FIELD_POLICIES,
    apply_model_update,
    append_items,
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

