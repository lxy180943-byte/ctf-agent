import json
from pathlib import Path

from ctf_agent.core.models import Challenge
from ctf_agent.core.orchestrator import Orchestrator
from ctf_agent.core.state import ChallengeState
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.graph.state import initial_workflow_state


def _setup(tmp_path: Path, challenge_id: str = "graph-map"):
    manager = WorkspaceManager(tmp_path / "workspace")
    challenge = Challenge(id=challenge_id, title="Graph Map", category="misc")
    state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    orchestrator = Orchestrator({"workspace_dir": str(tmp_path / "workspace")}, brain="graph")
    return orchestrator, state, layout


def _state(challenge: Challenge, layout, **updates):
    workflow_state = initial_workflow_state(challenge, run_dir=layout.challenge_dir, max_iterations=4)
    workflow_state.update(updates)
    return workflow_state


def _assert_saved(layout, expected_state: ChallengeState):
    saved = json.loads(layout.state_path.read_text(encoding="utf-8"))
    assert saved["state"] == expected_state.value
    assert "graph" in saved["metadata"]
    return saved


def _metadata_text(state) -> str:
    return json.dumps(state.metadata, ensure_ascii=False, sort_keys=True)


def test_paused_workflow_state_maps_to_paused_and_serializable_metadata(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    orchestrator, state, layout = _setup(tmp_path, "graph-map-paused")
    workflow_state = _state(
        state.challenge,
        layout,
        phase="paused",
        iteration=2,
        paused=True,
        pause_reason="Authorization: Bearer sk-secret-value",
        pending_human_question="Need scope",
        observations=[{"source": "pause_for_human", "raw": "x" * 5000, "token": "sk-secret-value"}],
        hypotheses=[{"name": "pause"}],
        current_hypothesis="pause",
        candidate_chains=[{"steps": ["ask"]}],
        tool_calls=[{"action_type": "pause", "status": "paused"}],
    )

    metadata = orchestrator._apply_graph_result(state, workflow_state, layout)

    assert state.state is ChallengeState.PAUSED
    assert metadata["brain"] == "graph"
    assert metadata["graph_terminal_phase"] == "paused"
    assert metadata["iteration_count"] == 2
    assert metadata["tool_call_count"] == 1
    assert metadata["hypothesis_count"] == 1
    assert metadata["graph_paused"] is True
    assert metadata["graph_solved"] is False
    json.dumps(state.metadata, ensure_ascii=False)
    text = _metadata_text(state)
    assert "sk-secret-value" not in text
    assert "Authorization: Bearer sk-secret-value" not in text
    assert "<truncated" in text
    _assert_saved(layout, ChallengeState.PAUSED)


def test_verified_solved_workflow_state_maps_to_solved_with_verified_candidate(tmp_path: Path):
    orchestrator, state, layout = _setup(tmp_path, "graph-map-solved")
    workflow_state = _state(
        state.challenge,
        layout,
        phase="finished",
        solved=True,
        verified_candidates=[{"value": "flag{verified}", "source": "verifier", "confidence": 0.95, "verified": True}],
        events=[{"node": "verify_candidates", "status": "ok"}],
    )

    metadata = orchestrator._apply_graph_result(state, workflow_state, layout)

    assert state.state is ChallengeState.SOLVED
    assert [candidate.value for candidate in state.flag_candidates] == ["flag{verified}"]
    assert state.flag_candidates[0].verified is True
    assert metadata["graph_solved"] is True
    _assert_saved(layout, ChallengeState.SOLVED)


def test_unverified_flag_text_does_not_map_to_solved(tmp_path: Path):
    orchestrator, state, layout = _setup(tmp_path, "graph-map-unverified")
    workflow_state = _state(
        state.challenge,
        layout,
        phase="finished",
        solved=True,
        observations=[{"source": "model", "text": "maybe flag{unverified}"}],
        verified_candidates=[{"value": "flag{unverified}", "source": "model", "confidence": 0.9, "verified": False}],
        events=[{"node": "reason_about_challenge", "status": "ok"}],
    )

    metadata = orchestrator._apply_graph_result(state, workflow_state, layout)

    assert state.state is ChallengeState.ANALYZING
    assert state.flag_candidates == []
    assert metadata["graph_solved"] is False
    _assert_saved(layout, ChallengeState.ANALYZING)


def test_failure_workflow_state_maps_to_failed(tmp_path: Path):
    orchestrator, state, layout = _setup(tmp_path, "graph-map-failed")
    workflow_state = _state(
        state.challenge,
        layout,
        phase="failed",
        failure_reason="graph stopped",
        failed_actions=[{"reason": "bad action"}],
    )

    metadata = orchestrator._apply_graph_result(state, workflow_state, layout)

    assert state.state is ChallengeState.FAILED
    assert metadata["graph_failure_reason"] == "graph stopped"
    assert state.metadata["graph"]["failed_actions"] == [{"reason": "bad action"}]
    _assert_saved(layout, ChallengeState.FAILED)
