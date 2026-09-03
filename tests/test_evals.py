import json
from pathlib import Path

from ctf_agent.cli.app import main
from ctf_agent.evals import BenchmarkRunner, CyberZeroAdapter, CybenchAdapter, LocalBenchmark, NYUCTFBenchAdapter
from ctf_agent.memory import MemoryStore


def make_dataset(root: Path) -> Path:
    dataset = root / "dataset"
    for challenge_id, category, flag, tags in [
        ("crypto-basic", "crypto", "flag{crypto_toy}", ["encoding", "fast"]),
        ("forensics-basic", "forensics", "flag{forensics_toy}", ["metadata"]),
        ("web-basic", "web", "flag{web_toy}", ["http", "fast"]),
    ]:
        challenge_dir = dataset / challenge_id
        challenge_dir.mkdir(parents=True)
        (challenge_dir / "challenge.yaml").write_text(
            "\n".join(
                [
                    f"id: {challenge_id}",
                    f"title: {challenge_id}",
                    f"category: {category}",
                    "description: toy benchmark",
                    "files:",
                    "  - flag.txt",
                    "flag_regex: flag\\{[A-Za-z0-9_]+\\}",
                    "metadata:",
                    f"  expected_flag: {flag}",
                    "  max_time: 30",
                    "  difficulty: easy",
                    "  tags:",
                    *[f"    - {tag}" for tag in tags],
                    "  required_tools:",
                    "    - python3",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (challenge_dir / "flag.txt").write_text(flag + "\n", encoding="utf-8")
    return dataset


def test_local_benchmark_reads_mature_metadata(tmp_path):
    dataset = make_dataset(tmp_path)
    challenges = LocalBenchmark(dataset).list_challenges()
    assert [item.challenge.id for item in challenges] == ["crypto-basic", "forensics-basic", "web-basic"]
    assert challenges[0].expected_flags == ["flag{crypto_toy}"]
    assert challenges[0].max_time == 30.0
    assert challenges[0].difficulty == "easy"
    assert challenges[0].tags == ["encoding", "fast"]
    assert challenges[0].required_tools == ["python3"]


def test_benchmark_runner_writes_scorecards_results_and_report(tmp_path):
    dataset = make_dataset(tmp_path)
    output = tmp_path / "eval-output"
    config = {
        "workspace_dir": str(tmp_path / "workspace"),
        "sandbox": {"engine": "local", "timeout_seconds": 10},
        "memory": {"enabled": False, "auto_learn": False, "path": str(tmp_path / "memory.sqlite")},
    }
    summary = BenchmarkRunner(config, max_steps=20, executor_name="local", output_dir=output, brain="fallback").run(LocalBenchmark(dataset))
    assert summary.metrics()["solved_count"] == 3
    assert summary.metrics()["command_count"] == 6
    assert summary.metrics()["verifier_false_positive"] == 0
    assert summary.metrics()["resume_success"] == 3
    assert summary.results[0].scorecard.solved is True
    assert "python3" in summary.results[0].scorecard.tools_used
    report = (output / "eval_report.md").read_text(encoding="utf-8")
    assert "stuck_stage" in report
    assert "tools_used" in report
    result_lines = (output / "eval_results.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(result_lines) == 3
    first = json.loads(result_lines[0])
    assert first["expected_flags"] == ["flag{crypto_toy}"]
    assert first["difficulty"] == "easy"
    assert first["scorecard"]["solved"] is True


def test_benchmark_runner_marks_false_positive_with_failure_trace_summary(tmp_path):
    dataset = make_dataset(tmp_path)
    challenge_yaml = dataset / "crypto-basic" / "challenge.yaml"
    challenge_yaml.write_text(challenge_yaml.read_text(encoding="utf-8").replace("flag{crypto_toy}", "flag{expected_other}"), encoding="utf-8")
    summary = BenchmarkRunner(
        {
            "workspace_dir": str(tmp_path / "workspace"),
            "sandbox": {"engine": "local", "timeout_seconds": 10},
            "memory": {"enabled": False, "auto_learn": False},
        },
        executor_name="local",
        output_dir=tmp_path / "eval-output",
        brain="fallback",
    ).run(LocalBenchmark(dataset))
    assert summary.metrics()["solved_count"] == 2
    assert summary.metrics()["verifier_false_positive"] == 1
    failed = [result for result in summary.results if result.verifier_false_positive][0]
    assert failed.scorecard.stuck_stage == "verifying"
    assert failed.scorecard.trace_summary
    report = (tmp_path / "eval-output" / "eval_report.md").read_text(encoding="utf-8")
    assert "Trace summary:" in report
    assert "Next suggestions:" in report


def test_benchmark_filters_fail_fast_and_repeat_regression(tmp_path):
    dataset = make_dataset(tmp_path)
    summary = BenchmarkRunner(
        {"workspace_dir": str(tmp_path / "workspace"), "sandbox": {"engine": "local", "timeout_seconds": 10}, "memory": {"enabled": False}},
        max_steps=20,
        executor_name="local",
        output_dir=tmp_path / "eval-output",
        only_category="crypto",
        only_tag="encoding",
        repeat=2,
        regression=True,
    ).run(LocalBenchmark(dataset))
    assert len(summary.results) == 2
    assert {result.repeat_index for result in summary.results} == {1, 2}
    assert summary.metrics()["unique_challenge_count"] == 1
    assert len(summary.regression) == 2
    assert summary.regression[0].solved_delta == 0

    bad_yaml = dataset / "web-basic" / "challenge.yaml"
    bad_yaml.write_text(bad_yaml.read_text(encoding="utf-8").replace("flag{web_toy}", "flag{wrong_expected}"), encoding="utf-8")
    fail_fast = BenchmarkRunner(
        {"workspace_dir": str(tmp_path / "workspace2"), "sandbox": {"engine": "local", "timeout_seconds": 10}, "memory": {"enabled": False}},
        max_steps=20,
        executor_name="local",
        output_dir=tmp_path / "eval-output2",
        brain="fallback",
        only_tag="http",
        fail_fast=True,
    ).run(LocalBenchmark(dataset))
    assert len(fail_fast.results) == 1
    assert fail_fast.results[0].solved is False


def test_eval_cli_runs_filters_and_repeat(capsys, tmp_path, monkeypatch):
    dataset = make_dataset(tmp_path)
    output = tmp_path / "eval-output"
    monkeypatch.setenv("CTF_AGENT_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("CTF_AGENT_MEMORY_ENABLED", "false")
    assert main(["eval", str(dataset), "--brain", "fallback", "--executor", "local", "--max-steps", "20", "--only-category", "crypto", "--only-tag", "encoding", "--repeat", "2", "--output-dir", str(output)]) == 0
    text = capsys.readouterr().out
    assert "solved_count: 2/2" in text
    data = json.loads((output / "eval_summary.json").read_text(encoding="utf-8"))
    assert data["metrics"]["repeat"] == 2
    assert data["filters"]["only_category"] == "crypto"
    assert data["regression"]


def test_eval_writes_benchmark_memory_scope(tmp_path):
    dataset = make_dataset(tmp_path)
    memory = tmp_path / "memory.sqlite"
    config = {
        "workspace_dir": str(tmp_path / "workspace"),
        "sandbox": {"engine": "local", "timeout_seconds": 10},
        "memory": {"enabled": True, "auto_learn": False, "path": str(memory)},
    }
    BenchmarkRunner(config, max_steps=20, executor_name="local", output_dir=tmp_path / "eval-output", brain="fallback").run(LocalBenchmark(dataset))
    items = MemoryStore(memory).list(limit=20)
    assert items
    assert {item.metadata["experience_scope"] for item in items} == {"benchmark"}
    assert all(item.metadata["kind"] == "eval-benchmark-result" for item in items)


def test_reserved_benchmark_adapters_conversion_skeleton(tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "id": "external-1",
                "title": "External",
                "category": "crypto",
                "description": "converted",
                "files": ["cipher.txt"],
                "expected_flags": ["flag{external}"],
                "difficulty": "medium",
                "tags": ["imported"],
                "required_tools": ["python3"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "cipher.txt").write_text("flag{external}\n", encoding="utf-8")
    for adapter_cls in (CybenchAdapter, NYUCTFBenchAdapter, CyberZeroAdapter):
        adapter = adapter_cls(tmp_path)
        out = adapter.convert_to_local_dataset(tmp_path / adapter.name)
        yaml_text = (out / "external-1" / "challenge.yaml").read_text(encoding="utf-8")
        assert "source_benchmark" in yaml_text
        assert (out / "external-1" / "cipher.txt").exists()

    for adapter in (CybenchAdapter(), NYUCTFBenchAdapter(), CyberZeroAdapter()):
        try:
            adapter.list_challenges()
        except NotImplementedError as exc:
            assert "future external benchmark" in str(exc)
