import json
from pathlib import Path

from ctf_agent.cli.app import main
from ctf_agent.core import maturity


def test_build_maturity_report_classifies_from_patched_signals(monkeypatch):
    monkeypatch.setattr(maturity, "_llm_section", lambda config: {
        "provider": "openai-compatible",
        "model": "gpt-test",
        "base_url": "https://llm.example/v1",
        "timeout_seconds": 60,
        "api_key_present": True,
        "real_provider": True,
        "ok": True,
        "error": None,
        "recommendation": None,
    })
    monkeypatch.setattr(maturity, "_tools_section", lambda: {
        "ok": True,
        "total": 10,
        "available": 10,
        "missing": 0,
        "available_ratio": 1.0,
        "missing_tools": [],
        "notes": [],
    })
    monkeypatch.setattr(maturity, "_docker_section", lambda: {
        "docker_available": True,
        "ok": True,
        "ready_profiles": ["generic", "web"],
        "missing_profiles": [],
        "profiles": [],
    })
    monkeypatch.setattr(maturity, "_benchmark_section", lambda config, eval_summary=None, eval_summary_path=None: {
        "summary_path": "/tmp/eval_summary.json",
        "dataset": "local",
        "challenge_count": 4,
        "solved_count": 4,
        "pass_rate": 1.0,
        "false_positive_rate": 0.0,
        "verifier_false_positive": 0,
        "metrics": {"challenge_count": 4, "solved_count": 4, "verifier_false_positive": 0},
        "capability_gaps": {"weak_categories": []},
    })
    monkeypatch.setattr(maturity, "_memory_section", lambda config: {
        "enabled": True,
        "ok": True,
        "total_items": 8,
        "traceable_ratio": 1.0,
        "avg_confidence": 0.88,
        "quality_score": 0.91,
        "notes": [],
    })
    monkeypatch.setattr(maturity, "_ui_section", lambda config: {
        "ok": True,
        "health_url": "http://127.0.0.1:1/api/health",
        "payload": {"ok": True},
    })
    monkeypatch.setattr(maturity, "_safety_section", lambda config, llm: {
        "ok": True,
        "dry_run_default": True,
        "allow_network_default": False,
        "llm_env_only": True,
        "trace_redaction": True,
        "notes": [],
    })

    report = maturity.build_maturity_report({})
    assert report["level"] == "mature"
    assert report["missing_to_mature"] == []
    assert report["llm"]["real_provider"] is True


def test_write_maturity_report_writes_markdown(tmp_path, monkeypatch):
    monkeypatch.setattr(maturity, "_llm_section", lambda config: {
        "provider": "none",
        "model": None,
        "base_url": None,
        "timeout_seconds": 60,
        "api_key_present": False,
        "real_provider": False,
        "ok": True,
        "error": None,
        "recommendation": None,
    })
    monkeypatch.setattr(maturity, "_tools_section", lambda: {
        "ok": True,
        "total": 2,
        "available": 1,
        "missing": 1,
        "available_ratio": 0.5,
        "missing_tools": ["generic/rg"],
        "notes": ["install ripgrep"],
    })
    monkeypatch.setattr(maturity, "_docker_section", lambda: {
        "docker_available": False,
        "ok": False,
        "ready_profiles": [],
        "missing_profiles": ["web"],
        "profiles": [],
    })
    monkeypatch.setattr(maturity, "_benchmark_section", lambda config, eval_summary=None, eval_summary_path=None: {
        "summary_path": None,
        "dataset": None,
        "challenge_count": 0,
        "solved_count": 0,
        "pass_rate": None,
        "false_positive_rate": 0.0,
        "verifier_false_positive": 0,
        "metrics": {},
        "capability_gaps": {},
    })
    monkeypatch.setattr(maturity, "_memory_section", lambda config: {
        "enabled": False,
        "ok": False,
        "total_items": 0,
        "traceable_ratio": 0.0,
        "avg_confidence": 0.0,
        "quality_score": 0.0,
        "notes": ["memory is disabled in config"],
    })
    monkeypatch.setattr(maturity, "_ui_section", lambda config: {
        "ok": False,
        "health_url": None,
        "error": "boom",
    })
    monkeypatch.setattr(maturity, "_safety_section", lambda config, llm: {
        "ok": False,
        "dry_run_default": False,
        "allow_network_default": True,
        "llm_env_only": False,
        "trace_redaction": True,
        "notes": ["unsafe"],
    })

    output = maturity.write_maturity_report({}, output_path=tmp_path / "maturity.md")
    text = output.read_text(encoding="utf-8")
    assert output.exists()
    assert "CTF Agent Maturity Report" in text
    assert "Missing To Mature" in text


def test_eval_cli_updates_maturity_report(tmp_path, monkeypatch):
    dataset = tmp_path / "dataset"
    challenge = dataset / "demo"
    challenge.mkdir(parents=True)
    (challenge / "challenge.yaml").write_text(
        "id: demo\ntitle: demo\ncategory: misc\nfiles:\n  - flag.txt\nflag_regex: flag\\{[A-Za-z0-9_]+\\}\nmetadata:\n  expected_flag: flag{demo}\n",
        encoding="utf-8",
    )
    (challenge / "flag.txt").write_text("flag{demo}\n", encoding="utf-8")

    captured = {}

    def fake_write(config, *, eval_summary=None, eval_summary_path=None, output_path=None):
        captured["summary"] = eval_summary
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("patched maturity", encoding="utf-8")
        return output

    monkeypatch.setenv("CTF_AGENT_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("CTF_AGENT_MEMORY_ENABLED", "false")
    monkeypatch.setattr("ctf_agent.cli.app.write_maturity_report", fake_write)

    maturity_output = tmp_path / "maturity" / "report.md"
    assert main(["eval", str(dataset), "--brain", "fallback", "--executor", "local", "--max-steps", "5", "--output-dir", str(tmp_path / "eval-output"), "--maturity-output", str(maturity_output)]) == 0
    assert maturity_output.exists()
    assert captured["summary"] is not None
