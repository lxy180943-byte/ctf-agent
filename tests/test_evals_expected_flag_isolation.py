from __future__ import annotations

import json
from pathlib import Path

from ctf_agent.core.models import Challenge
from ctf_agent.core.orchestrator import SolveResult
from ctf_agent.core.state import ChallengeState
from ctf_agent.evals import BenchmarkRunner, LocalBenchmark, sanitize_benchmark_for_solver
from ctf_agent.graph.context import build_evidence_packet
from ctf_agent.pydantic_agent.agent import SolverDependencies
from ctf_agent.memory import MemoryStore

EXPECTED = "flag{isolated_expected_secret}"


def _dataset(root: Path) -> Path:
    dataset = root / "reasoning"
    case = dataset / "source-disclosure"
    case.mkdir(parents=True)
    (case / "challenge.yaml").write_text(
        "\n".join(
            [
                "id: source-disclosure",
                "title: Source Disclosure",
                "category: web",
                f"description: public description without {EXPECTED}",
                "files:",
                "  - public.txt",
                "  - answer.txt",
                "hints:",
                f"  - solver must not see {EXPECTED}",
                "metadata:",
                f"  expected_flag: {EXPECTED}",
                "  expected_flag_isolated: true",
                "  benchmark_type: reasoning",
                "  evaluator_only_files:",
                "    - answer.txt",
                "  solution: read answer.txt",
                "  ground_truth: hidden answer metadata",
                "  difficulty: easy",
                "  tags:",
                "    - reasoning",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (case / "public.txt").write_text("public artifact only\n", encoding="utf-8")
    (case / "answer.txt").write_text(EXPECTED + "\n", encoding="utf-8")
    return dataset


def _assert_no_expected(value) -> None:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) if not isinstance(value, str) else value
    assert EXPECTED not in text
    assert "answer.txt" not in text
    assert "ground_truth" not in text
    assert "solution" not in text


def test_local_reasoning_case_keeps_private_expected_flag_but_sanitizes_solver_challenge(tmp_path):
    item = LocalBenchmark(_dataset(tmp_path)).list_challenges()[0]

    assert item.expected_flags == [EXPECTED]
    assert item.evaluator_metadata["expected_flag"] == EXPECTED
    assert item.challenge.files == ["public.txt"]
    _assert_no_expected(item.challenge.to_dict())
    assert item.challenge.hints == []

    sanitized = sanitize_benchmark_for_solver(item)
    _assert_no_expected(sanitized.to_dict())


def test_workspace_input_and_evidence_dependencies_do_not_receive_expected_flag(tmp_path):
    item = LocalBenchmark(_dataset(tmp_path)).list_challenges()[0]
    work_dir = tmp_path / "workspace-input"
    item.adapter.download_files(item.challenge, work_dir)

    assert (work_dir / "public.txt").exists()
    assert not (work_dir / "answer.txt").exists()

    workflow_state = {
        "challenge": item.challenge.to_dict(),
        "run_dir": str(tmp_path / "run"),
        "confirmed_facts": [],
        "constraints": [],
        "anomalies": [],
        "hypotheses": [],
        "experiments": [],
        "observations": [],
        "artifacts": [],
        "verified_candidates": [],
        "memory_matches": [],
        "skill_notes": [],
    }
    packet = build_evidence_packet(workflow_state, challenge=item.challenge.to_dict(), trace_events=[], memory=[], skills=[], tools=[], network_scope={}, limits={})
    deps = SolverDependencies(challenge=item.challenge.to_dict(), evidence_packet=packet.model_dump(mode="json"))

    _assert_no_expected(packet.model_dump(mode="json"))
    _assert_no_expected(deps.__dict__)


def test_runner_scores_with_private_expected_flag_without_leaking_outputs_or_memory(tmp_path, monkeypatch):
    dataset = _dataset(tmp_path)
    captured_challenges = []
    run_dir = tmp_path / "run"

    class FakeOrchestrator:
        def __init__(self, *args, **kwargs):
            pass

        def solve(self, challenge: Challenge, *, adapter=None):
            captured_challenges.append(challenge.to_dict())
            if adapter is not None:
                adapter.download_files(challenge, run_dir / "work")
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "state.json").write_text("{}", encoding="utf-8")
            return SolveResult(challenge_id=challenge.id, state=ChallengeState.SOLVED, flags=[EXPECTED], run_dir=run_dir, steps_executed=1, metadata={"brain": "test"})

        def resume_from_run_dir(self, _run_dir):
            return SolveResult(challenge_id="source-disclosure", state=ChallengeState.SOLVED, flags=[EXPECTED], run_dir=run_dir, steps_executed=1)

    monkeypatch.setattr("ctf_agent.evals.runner.Orchestrator", FakeOrchestrator)
    memory = tmp_path / "memory.sqlite"
    output = tmp_path / "out"
    config = {"workspace_dir": str(tmp_path / "workspace"), "memory": {"enabled": True, "auto_learn": False, "path": str(memory)}}

    summary = BenchmarkRunner(config, output_dir=output, brain="fallback", write_memory=True).run(LocalBenchmark(dataset))

    assert summary.results[0].expected_flag_matched is True
    assert summary.results[0].solve_success is True
    assert summary.results[0].solved is True
    assert summary.results[0].verifier_false_positive is False
    _assert_no_expected(captured_challenges[0])
    assert not (run_dir / "work" / "answer.txt").exists()

    result_text = (output / "eval_results.jsonl").read_text(encoding="utf-8")
    report_text = (output / "eval_report.md").read_text(encoding="utf-8")
    summary_text = (output / "eval_summary.json").read_text(encoding="utf-8")
    for text in (result_text, report_text, summary_text):
        assert EXPECTED not in text
        assert "expected_flags" not in text
        assert "expected_flag_matched" in text

    memory_text = json.dumps([item.to_dict() for item in MemoryStore(memory).list(limit=10)], ensure_ascii=False, sort_keys=True)
    assert EXPECTED not in memory_text


def test_unverified_guessed_expected_flag_does_not_count_as_success(tmp_path, monkeypatch):
    dataset = _dataset(tmp_path)

    class GuessingOrchestrator:
        def __init__(self, *args, **kwargs):
            pass

        def solve(self, challenge: Challenge, *, adapter=None):
            return SolveResult(challenge_id=challenge.id, state=ChallengeState.ANALYZING, flags=[EXPECTED], run_dir=None, steps_executed=1)

    monkeypatch.setattr("ctf_agent.evals.runner.Orchestrator", GuessingOrchestrator)
    summary = BenchmarkRunner({"workspace_dir": str(tmp_path / "workspace"), "memory": {"enabled": False}}, output_dir=tmp_path / "out", brain="fallback").run(LocalBenchmark(dataset))

    result = summary.results[0]
    assert result.expected_flag_matched is True
    assert result.solve_success is False
    assert result.solved is False
    assert result.verifier_false_positive is False
    assert EXPECTED not in (tmp_path / "out" / "eval_results.jsonl").read_text(encoding="utf-8")
