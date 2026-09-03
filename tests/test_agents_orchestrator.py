import json
from pathlib import Path

from ctf_agent.agents import AgentContext, ExecutionBatch, PlannerAgent, VerifierAgent
from ctf_agent.core.models import Artifact, Challenge
from ctf_agent.core.orchestrator import Orchestrator
from ctf_agent.core.state import ChallengeRunState, ChallengeState
from ctf_agent.core.trace import TraceStore
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.platforms.local import LocalPlatformAdapter
from ctf_agent.sandbox import ExecutionResult, LocalExecutor
from ctf_agent.tools import default_registry


def test_planner_generates_python_scan_commands(tmp_path):
    challenge = Challenge(id="toy", title="Toy", category="misc", description="Find it", files=["flag.txt"])
    manager = WorkspaceManager(tmp_path)
    state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    context = AgentContext(
        state=state,
        layout=layout,
        trace_store=manager.trace_store_for(challenge.id),
        executor=LocalExecutor(tmp_path),
        tool_registry=default_registry(),
        config={},
        max_steps=10,
        timeout=30,
    )
    plan = PlannerAgent().run(context)
    assert plan.commands
    assert "python3 -c" in plan.commands[-1].command
    assert "recommended_tools" in plan.metadata


def test_verifier_extracts_flag_from_result_and_artifact(tmp_path):
    artifact = Artifact(path=str(tmp_path / "stdout.txt"), kind="stdout")
    Path(artifact.path).write_text("flag{artifact_demo}", encoding="utf-8")
    result = ExecutionResult(
        command="cat stdout.txt",
        cwd=str(tmp_path),
        env={},
        timeout=10,
        exit_code=0,
        stdout="no flag here",
        stderr="",
        started_at="2026-08-31T00:00:00Z",
        ended_at="2026-08-31T00:00:01Z",
        duration_seconds=1.0,
        artifacts=[artifact],
    )
    challenge = Challenge(id="toy", title="Toy", category="misc")
    manager = WorkspaceManager(tmp_path / "workspace")
    state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    context = AgentContext(
        state=state,
        layout=layout,
        trace_store=manager.trace_store_for(challenge.id),
        executor=LocalExecutor(tmp_path / "workspace"),
        tool_registry=default_registry(),
        config={},
        max_steps=10,
        timeout=30,
        metadata={"execution_batch": ExecutionBatch(results=[result])},
    )
    verification = VerifierAgent().run(context)
    assert verification.solved is True
    assert verification.candidates[0].value == "flag{artifact_demo}"


def test_orchestrator_solves_local_toy_challenge(tmp_path):
    challenge_dir = tmp_path / "challenge1"
    challenge_dir.mkdir()
    (challenge_dir / "challenge.yaml").write_text(
        "title: Toy\ncategory: misc\nfiles:\n  - flag.txt\nflag_regex: flag\\{[^}]+\\}\n",
        encoding="utf-8",
    )
    (challenge_dir / "flag.txt").write_text("flag{mvp_loop}\n", encoding="utf-8")

    adapter = LocalPlatformAdapter(challenge_dir)
    challenge = adapter.get_challenge(str(challenge_dir))
    orchestrator = Orchestrator(
        {"workspace_dir": str(tmp_path / "workspace"), "sandbox": {"engine": "local", "timeout_seconds": 10}},
        executor_name="local",
        brain="fallback",
        max_steps=10,
    )
    result = orchestrator.solve(challenge, adapter=adapter)

    assert result.solved is True
    assert result.flags == ["flag{mvp_loop}"]
    assert result.state is ChallengeState.SOLVED
    assert (result.run_dir / "state.json").exists()
    assert (result.run_dir / "trace.jsonl").exists()


def test_orchestrator_resume_solved_run(tmp_path):
    challenge_dir = tmp_path / "challenge1"
    challenge_dir.mkdir()
    (challenge_dir / "challenge.yaml").write_text("title: Toy\ncategory: misc\nfiles:\n  - flag.txt\n", encoding="utf-8")
    (challenge_dir / "flag.txt").write_text("flag{resume_demo}\n", encoding="utf-8")
    config = {"workspace_dir": str(tmp_path / "workspace"), "sandbox": {"engine": "local", "timeout_seconds": 10}}
    adapter = LocalPlatformAdapter(challenge_dir)
    challenge = adapter.get_challenge(str(challenge_dir))
    first = Orchestrator(config, executor_name="local", brain="fallback").solve(challenge, adapter=adapter)

    resumed = Orchestrator(config, executor_name="local", brain="fallback").resume_from_run_dir(first.run_dir)

    assert resumed.solved is True
    assert resumed.flags == ["flag{resume_demo}"]
    assert resumed.steps_executed == 0


def test_state_file_contains_flag_candidate_after_solve(tmp_path):
    challenge_dir = tmp_path / "challenge1"
    challenge_dir.mkdir()
    (challenge_dir / "challenge.yaml").write_text("title: Toy\ncategory: misc\nfiles:\n  - flag.txt\n", encoding="utf-8")
    (challenge_dir / "flag.txt").write_text("flag{state_demo}\n", encoding="utf-8")
    config = {"workspace_dir": str(tmp_path / "workspace"), "sandbox": {"engine": "local", "timeout_seconds": 10}}
    adapter = LocalPlatformAdapter(challenge_dir)
    result = Orchestrator(config, executor_name="local", brain="fallback").solve(adapter.get_challenge(str(challenge_dir)), adapter=adapter)
    data = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
    assert data["state"] == "solved"
    assert data["attempts"][0]["ended_at"] is not None
    assert data["flag_candidates"][0]["value"] == "flag{state_demo}"
