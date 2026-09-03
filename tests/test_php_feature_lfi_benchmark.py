import json
from pathlib import Path

from ctf_agent.evals import BenchmarkRunner, LocalBenchmark
from ctf_agent.llm import DummyProvider


def test_php_feature_lfi_local_benchmark_loads_expected_flag(tmp_path):
    dataset = Path(__file__).resolve().parents[1] / "evals" / "datasets" / "local" / "php-feature-lfi"
    benchmark = LocalBenchmark(dataset)
    challenges = benchmark.list_challenges()
    assert len(challenges) == 1
    item = challenges[0]
    assert item.challenge.id == "php-feature-lfi"
    assert item.expected_flags == ["flag{php_feature_lfi_local}"]
    assert item.challenge.metadata["source_benchmark"] == "local-php-feature-lfi"


def test_php_feature_lfi_local_benchmark_runs_through_hybrid_loop(tmp_path):
    dataset = Path(__file__).resolve().parents[1] / "evals" / "datasets" / "local" / "php-feature-lfi"
    provider = DummyProvider(
        [
            '{"hypothesis":"source leak reveals parse_str, md5 weak compare, in_array loose comparison, intval base 0, and include filename blacklist","evidence_used":["source.txt highlight output"],"uncertainty":["need the exact flag file after source analysis"],"next_actions":[{"type":"read_file","path":"source.txt","reason":"recover PHP source from highlight output"}]}',
            '{"hypothesis":"source analysis indicates the local flag file is the next evidence to verify","evidence_used":["PHP source analysis JSON","source.txt"],"uncertainty":["need to confirm the final flag string"],"next_actions":[{"type":"read_file","path":"flag.txt","reason":"confirm the flag from the local benchmark"}]}',
            '{"hypothesis":"flag is present in the last evidence batch","evidence_used":["flag.txt"],"uncertainty":[],"next_actions":[{"type":"ask_verifier","reason":"verify the candidate from the local benchmark"}]}',
        ]
    )
    config = {
        "workspace_dir": str(tmp_path / "workspace"),
        "sandbox": {"engine": "local", "timeout_seconds": 10},
        "memory": {"enabled": False, "auto_learn": False, "path": str(tmp_path / "memory.sqlite")},
    }
    summary = BenchmarkRunner(
        config,
        max_steps=10,
        executor_name="local",
        output_dir=tmp_path / "eval-output",
        llm_provider=provider,
        brain="llm",
    ).run(LocalBenchmark(dataset))

    assert summary.metrics()["solved_count"] == 1
    result = summary.results[0]
    assert result.solved is True
    assert result.expected_flags == ["flag{php_feature_lfi_local}"]
    assert result.metadata["solve"]["brain_mode"] == "llm"
    assert result.metadata["solve"]["loop"] == "interactive-llm"
    assert result.scorecard.evidence_steps

    report = (tmp_path / "eval-output" / "eval_report.md").read_text(encoding="utf-8")
    assert "Evidence steps:" in report
    assert "source.txt" in report
    assert "flag.txt" in report
    assert "interactive-llm" in json.dumps(result.metadata, ensure_ascii=False)
