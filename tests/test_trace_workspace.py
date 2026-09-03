import json
from pathlib import Path

from ctf_agent.core.models import Artifact, Challenge, Step
from ctf_agent.core.state import ChallengeState
from ctf_agent.core.trace import TraceEvent, TraceStore, summarize_text
from ctf_agent.core.workspace import WorkspaceManager, slugify


def test_summarize_text_truncates_long_output():
    summary = summarize_text("A" * 20, limit=8)
    assert summary == "A" * 8 + "\n... <truncated 12 chars>"


def test_trace_store_appends_jsonl_event(tmp_path):
    trace = TraceStore(tmp_path / "trace.jsonl")
    event = TraceEvent(
        challenge_id="crypto-1",
        agent="executor",
        action="run-command",
        command=["python3", "solve.py"],
        stdout="ok",
        stderr="",
        artifacts=[Artifact(path="out.txt")],
        exit_code=0,
    )
    trace.append(event)

    lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["agent"] == "executor"
    assert data["artifacts"][0]["path"] == "out.txt"
    assert trace.read_events()[0].exit_code == 0


def test_trace_store_appends_step(tmp_path):
    step = Step(agent="executor", action="strings", command=["strings", "chall"], exit_code=0)
    step.finish(exit_code=0)
    trace = TraceStore(tmp_path / "trace.jsonl")
    event = trace.append_step("rev-1", step, stdout="flag-like text", stderr="")
    assert event.metadata["step_id"] == step.id
    assert trace.read_events()[0].stdout == "flag-like text"


def test_workspace_manager_prepares_per_challenge_layout(tmp_path):
    challenge = Challenge(id="Baby Rev++", title="Baby Rev", category="rev")
    manager = WorkspaceManager(tmp_path)
    layout = manager.prepare(challenge)

    assert layout.challenge_dir == tmp_path / "runs" / slugify("Baby Rev++")
    assert layout.input_dir.is_dir()
    assert layout.work_dir.is_dir()
    assert layout.artifacts_dir.is_dir()


def test_workspace_resume_restores_state_and_trace(tmp_path):
    challenge = Challenge(id="misc-1", title="Misc 1", category="misc")
    manager = WorkspaceManager(tmp_path)
    state = manager.init_state(challenge)
    state.transition_to(ChallengeState.ANALYZING)
    manager.save_state(state)

    trace = manager.trace_store_for(challenge.id)
    trace.append(TraceEvent(challenge_id=challenge.id, agent="planner", action="inspect"))

    resumed = manager.resume(challenge.id)
    assert resumed.state.state is ChallengeState.ANALYZING
    assert len(resumed.trace_events) == 1
    assert resumed.layout.state_path == Path(tmp_path) / "runs" / "misc-1" / "state.json"
