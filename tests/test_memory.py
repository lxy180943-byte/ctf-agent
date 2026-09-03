import json

import pytest

from ctf_agent.agents import AgentContext, PlannerAgent
from ctf_agent.cli.app import main
from ctf_agent.core.models import Challenge
from ctf_agent.core.orchestrator import Orchestrator
from ctf_agent.core.state import ChallengeState
from ctf_agent.core.trace import TraceEvent
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.memory import KnowledgeItem, MemoryStore
from ctf_agent.platforms.local import LocalPlatformAdapter
from ctf_agent.sandbox import LocalExecutor
from ctf_agent.tools import default_registry


def test_memory_store_requires_source_run(tmp_path):
    store = MemoryStore(tmp_path / "knowledge.sqlite")
    with pytest.raises(ValueError):
        store.add(
            KnowledgeItem(
                category="misc",
                pattern="pattern",
                symptom="symptom",
                solution="solution",
                commands=[],
                source_run="",
            )
        )


def test_memory_store_add_search_roundtrip(tmp_path):
    store = MemoryStore(tmp_path / "knowledge.sqlite")
    item = store.add(
        KnowledgeItem(
            category="crypto",
            pattern="RSA small modulus",
            symptom="n is factorable",
            solution="Factor n and reconstruct private key.",
            commands=["python3 solve.py"],
            source_run=str(tmp_path / "runs" / "rsa"),
            confidence=0.8,
        )
    )
    results = store.search("rsa factor", category="crypto")
    assert results[0].id == item.id
    assert results[0].source_run.endswith("rsa")


def test_memory_learns_from_solved_run(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspace")
    challenge = Challenge(id="learned", title="Learned Toy", category="misc", description="toy desc", files=["flag.txt"])
    state = manager.init_state(challenge)
    state.state = ChallengeState.SOLVED
    manager.save_state(state)
    trace = manager.trace_store_for("learned")
    trace.append(
        TraceEvent(
            challenge_id="learned",
            agent="executor",
            action="run-command",
            command=["bash", "-lc", "cat flag.txt"],
            stdout="flag{learned}",
            exit_code=0,
        )
    )
    state.add_flag_candidate(
        __import__("ctf_agent.core.models", fromlist=["FlagCandidate"]).FlagCandidate(
            value="flag{learned}",
            source="command:1:stdout",
            confidence=0.9,
            verified=True,
        )
    )
    manager.save_state(state)
    items = MemoryStore(tmp_path / "memory.sqlite").learn_from_run(manager.layout_for("learned").challenge_dir)
    assert items[0].metadata["kind"] == "solved-route"
    assert items[0].source_run == str(manager.layout_for("learned").challenge_dir.resolve())
    assert items[0].commands == ["cat flag.txt"]


def test_memory_learns_failure_retrospective(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspace")
    state = manager.init_state(Challenge(id="failed", title="Failed Toy", category="misc", files=["missing.txt"]))
    state.state = ChallengeState.FAILED
    state.metadata["failure_count"] = 1
    manager.save_state(state)
    manager.trace_store_for("failed").append(
        TraceEvent(
            challenge_id="failed",
            agent="executor",
            action="run-command",
            command=["bash", "-lc", "cat missing.txt"],
            stderr="No such file",
            exit_code=1,
        )
    )
    items = MemoryStore(tmp_path / "memory.sqlite").learn_from_run(manager.layout_for("failed").challenge_dir)
    assert items[0].metadata["kind"] == "failure-retrospective"
    assert items[0].metadata["invalid_commands"] == ["cat missing.txt"]
    assert items[0].source_run


def test_memory_learn_ignores_trace_events_older_than_state(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspace")
    state = manager.init_state(Challenge(id="fresh", title="Fresh Toy", category="misc", files=["flag.txt"]))
    manager.trace_store_for("fresh").append(
        TraceEvent(
            challenge_id="fresh",
            agent="executor",
            action="run-command",
            command=["bash", "-lc", "cat old-missing.txt"],
            exit_code=1,
            timestamp="2000-01-01T00:00:00Z",
        )
    )
    state.state = ChallengeState.SOLVED
    state.add_flag_candidate(
        __import__("ctf_agent.core.models", fromlist=["FlagCandidate"]).FlagCandidate(
            value="flag{fresh}",
            source="command:1:stdout",
            confidence=0.9,
            verified=True,
        )
    )
    manager.save_state(state)
    manager.trace_store_for("fresh").append(
        TraceEvent(
            challenge_id="fresh",
            agent="executor",
            action="run-command",
            command=["bash", "-lc", "cat flag.txt"],
            stdout="flag{fresh}",
            exit_code=0,
        )
    )
    items = MemoryStore(tmp_path / "memory.sqlite").learn_from_run(manager.layout_for("fresh").challenge_dir)
    assert len(items) == 1
    assert items[0].commands == ["cat flag.txt"]


def test_planner_retrieves_memory_before_planning(tmp_path):
    memory = MemoryStore(tmp_path / "knowledge.sqlite")
    memory.add(
        KnowledgeItem(
            category="misc",
            pattern="Toy text file",
            symptom="prompt contains demo flag",
            solution="Read prompt.txt first.",
            commands=["cat prompt.txt"],
            source_run=str(tmp_path / "workspace" / "runs" / "old"),
            confidence=0.8,
        )
    )
    manager = WorkspaceManager(tmp_path / "workspace")
    challenge = Challenge(id="toy", title="Toy", category="misc", description="demo flag", files=["prompt.txt"])
    state = manager.init_state(challenge)
    context = AgentContext(
        state=state,
        layout=manager.layout_for("toy"),
        trace_store=manager.trace_store_for("toy"),
        executor=LocalExecutor(tmp_path / "workspace"),
        tool_registry=default_registry(),
        config={"memory": {"enabled": True, "path": str(tmp_path / "knowledge.sqlite"), "search_limit": 5}},
        max_steps=10,
        timeout=30,
    )
    plan = PlannerAgent().run(context)
    assert plan.metadata["memory_matches"][0]["source_run"].endswith("old")
    assert "memory-search" in manager.layout_for("toy").trace_path.read_text(encoding="utf-8")


def test_orchestrator_auto_learns_after_solve(tmp_path):
    challenge_dir = tmp_path / "challenge1"
    challenge_dir.mkdir()
    (challenge_dir / "challenge.yaml").write_text("title: Toy\ncategory: misc\nfiles:\n  - flag.txt\n", encoding="utf-8")
    (challenge_dir / "flag.txt").write_text("flag{memory_auto}\n", encoding="utf-8")
    memory_path = tmp_path / "knowledge.sqlite"
    config = {
        "workspace_dir": str(tmp_path / "workspace"),
        "sandbox": {"engine": "local", "timeout_seconds": 10},
        "memory": {"enabled": True, "path": str(memory_path), "auto_learn": True, "search_limit": 5},
    }
    adapter = LocalPlatformAdapter(challenge_dir)
    result = Orchestrator(config, executor_name="local", brain="fallback").solve(adapter.get_challenge(str(challenge_dir)), adapter=adapter)
    assert result.solved is True
    assert result.metadata["learned"]
    assert MemoryStore(memory_path).search("Toy", category="misc")[0].source_run == str(result.run_dir.resolve())


def test_memory_cli_add_search_and_learn(capsys, tmp_path, monkeypatch):
    memory_path = tmp_path / "knowledge.sqlite"
    monkeypatch.setenv("CTF_AGENT_MEMORY_PATH", str(memory_path))
    source_run = tmp_path / "workspace" / "runs" / "manual"
    source_run.mkdir(parents=True)
    assert (
        main(
            [
                "memory",
                "add",
                "--category",
                "web",
                "--pattern",
                "login bypass",
                "--symptom",
                "SQL error",
                "--solution",
                "Try a quoted login payload in the authorized challenge.",
                "--command",
                "curl http://challenge.local",
                "--source-run",
                str(source_run),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["memory", "search", "login", "--category", "web", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["source_run"] == str(source_run)
