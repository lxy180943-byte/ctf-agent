import json

from ctf_agent.agents import AgentContext, PlannerAgent
from ctf_agent.cli.app import main
from ctf_agent.core.models import Challenge, FlagCandidate
from ctf_agent.core.reviewer import RunReviewer
from ctf_agent.core.state import ChallengeState
from ctf_agent.core.trace import TraceEvent
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.evals import BenchmarkRunner, LocalBenchmark
from ctf_agent.memory import KnowledgeItem, MemoryStore
from ctf_agent.sandbox import LocalExecutor
from ctf_agent.tools import default_registry


def solved_run(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspace")
    state = manager.init_state(Challenge(id="reviewed", title="Reviewed Toy", category="misc", files=["flag.txt"]))
    state.state = ChallengeState.SOLVED
    state.add_flag_candidate(FlagCandidate(value="flag{reviewed}", source="test", confidence=0.9, verified=True))
    manager.save_state(state)
    trace = manager.trace_store_for("reviewed")
    trace.append(TraceEvent(challenge_id="reviewed", agent="classifier", action="classify", stdout="misc"))
    trace.append(TraceEvent(challenge_id="reviewed", agent="executor", action="run-command", command=["bash", "-lc", "cat missing.txt"], stderr="missing", exit_code=1))
    trace.append(TraceEvent(challenge_id="reviewed", agent="executor", action="run-command", command=["bash", "-lc", "cat flag.txt"], stdout="flag{reviewed}", exit_code=0))
    return manager.layout_for("reviewed").challenge_dir


def test_review_run_writes_markdown_and_cli(tmp_path, capsys, monkeypatch):
    run_dir = solved_run(tmp_path)
    review = RunReviewer(tmp_path / "workspace").review(run_dir)
    assert (run_dir / "run_review.md").exists()
    assert review.effective_commands == ["cat flag.txt"]
    assert review.ineffective_commands
    monkeypatch.setenv("CTF_AGENT_WORKSPACE_DIR", str(tmp_path / "workspace"))
    assert main(["review-run", str(run_dir), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["challenge_id"] == "reviewed"


def test_memory_quality_promote_demote_prune_and_failure_scope(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    item = store.add(
        KnowledgeItem(
            category="crypto",
            pattern="rsa n factorable",
            symptom="ELF note irrelevant",
            solution="factor n",
            commands=["python3 solve.py"],
            source_run=str(tmp_path / "runs" / "rsa"),
            confidence=0.4,
        )
    )
    promoted = store.promote(item.id, amount=0.1)
    assert promoted.success_count == 1
    assert promoted.confidence > item.confidence
    demoted = store.demote(item.id, amount=0.2)
    assert demoted.failure_count == 1
    assert demoted.last_used

    low = store.add(
        KnowledgeItem(
            category="misc",
            pattern="failure-retrospective: bad route",
            symptom="bad",
            solution="avoid",
            commands=[],
            source_run=str(tmp_path / "runs" / "bad"),
            confidence=0.1,
            source_type="failure-retrospective",
            failure_count=1,
            metadata={"kind": "failure-retrospective"},
        )
    )
    assert low.source_type == "failure-retrospective"
    assert store.prune(min_confidence=0.2, source_type="failure-retrospective") == 1


def test_planner_weighted_memory_search_uses_magic_and_marks_last_used(tmp_path):
    memory = MemoryStore(tmp_path / "knowledge.sqlite")
    elf_item = memory.add(
        KnowledgeItem(
            category="pwn",
            pattern="ELF checksec route",
            symptom="ELF binary",
            solution="run file/checksec/readelf",
            commands=["file chall", "python3 solve.py"],
            source_run=str(tmp_path / "workspace" / "runs" / "old"),
            confidence=0.5,
            success_count=2,
        )
    )
    manager = WorkspaceManager(tmp_path / "workspace")
    challenge = Challenge(id="elf", title="Binary", category="pwn", files=["chall"])
    state = manager.init_state(challenge)
    layout = manager.layout_for("elf")
    (layout.work_dir / "chall").write_bytes(b"\x7fELF\x02\x01\x01")
    context = AgentContext(
        state=state,
        layout=layout,
        trace_store=manager.trace_store_for("elf"),
        executor=LocalExecutor(tmp_path / "workspace"),
        tool_registry=default_registry(),
        config={"memory": {"enabled": True, "path": str(tmp_path / "knowledge.sqlite"), "search_limit": 5}},
        max_steps=10,
        timeout=30,
    )
    plan = PlannerAgent().run(context)
    assert plan.metadata["memory_matches"][0]["id"] == elf_item.id
    assert MemoryStore(tmp_path / "knowledge.sqlite").get(elf_item.id).last_used


def test_memory_cli_promote_demote_prune(tmp_path, capsys, monkeypatch):
    memory_path = tmp_path / "knowledge.sqlite"
    monkeypatch.setenv("CTF_AGENT_MEMORY_PATH", str(memory_path))
    item = MemoryStore(memory_path).add(
        KnowledgeItem(
            category="misc",
            pattern="cli quality",
            symptom="demo",
            solution="demo",
            commands=[],
            source_run=str(tmp_path / "runs" / "cli"),
            confidence=0.19,
        )
    )
    assert main(["memory", "promote", item.id, "--json"]) == 0
    promoted = json.loads(capsys.readouterr().out)
    assert promoted["success_count"] == 1
    assert main(["memory", "demote", item.id, "--amount", "0.5", "--json"]) == 0
    demoted = json.loads(capsys.readouterr().out)
    assert demoted["failure_count"] == 1
    assert main(["memory", "prune", "--min-confidence", "0.2", "--include-successful", "--json"]) == 0
    pruned = json.loads(capsys.readouterr().out)
    assert pruned["deleted"] == 1


def test_eval_writes_capability_gap_report(tmp_path):
    dataset = tmp_path / "dataset"
    for challenge_id, category, expected in [("ok", "crypto", "flag{ok}"), ("fp", "web", "flag{expected}")]:
        challenge_dir = dataset / challenge_id
        challenge_dir.mkdir(parents=True)
        found = "flag{ok}" if challenge_id == "ok" else "flag{wrong}"
        (challenge_dir / "challenge.yaml").write_text(
            f"id: {challenge_id}\ntitle: {challenge_id}\ncategory: {category}\nfiles:\n  - flag.txt\nflag_regex: flag\\{{[A-Za-z0-9_]+\\}}\nmetadata:\n  expected_flag: {expected}\n  required_tools:\n    - definitely-missing-tool\n",
            encoding="utf-8",
        )
        (challenge_dir / "flag.txt").write_text(found + "\n", encoding="utf-8")
    output = tmp_path / "eval-output"
    summary = BenchmarkRunner(
        {"workspace_dir": str(tmp_path / "workspace"), "sandbox": {"engine": "local", "timeout_seconds": 10}, "memory": {"enabled": False}},
        executor_name="local",
        output_dir=output,
        brain="fallback",
    ).run(LocalBenchmark(dataset))
    assert summary.metrics()["verifier_false_positive"] == 1
    gaps = (output / "capability_gaps.md").read_text(encoding="utf-8")
    assert "web" in gaps
    assert "definitely-missing-tool" in gaps
    report = (output / "eval_report.md").read_text(encoding="utf-8")
    assert "Capability Gaps" in report
