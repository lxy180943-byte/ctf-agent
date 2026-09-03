import json
from pathlib import Path

import pytest

from ctf_agent import __version__
from ctf_agent.cli.app import build_parser, main


def test_version_command(capsys):
    assert main(["version"]) == 0
    assert f"ctf-agent {__version__}" in capsys.readouterr().out


def test_help_without_subcommand(capsys):
    assert main([]) == 0
    assert "Local-first CTF solving agent" in capsys.readouterr().out


def test_brain_parser_defaults_to_graph_and_accepts_modes():
    parser = build_parser()
    for command, target in (("solve", "challenge"), ("resume", "run-dir"), ("eval", "dataset")):
        assert parser.parse_args([command, target]).brain == "graph"
        for mode in ("graph", "fallback", "llm", "hybrid"):
            assert parser.parse_args([command, target, "--brain", mode]).brain == mode


def test_doctor_json_skips_docker_run(capsys):
    assert main(["doctor", "--json", "--skip-docker-run"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["system"]["is_linux"] is True
    assert report["docker"]["attempted"] is False


def test_doctor_executors_json(capsys):
    assert main(["doctor", "executors", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["local"]["ok"] is True
    assert "generic" in report["docker"]["images"]


def test_doctor_llm_json_redacts_key(capsys, monkeypatch):
    monkeypatch.setenv("CTF_AGENT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-openai-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
    assert main(["doctor", "llm", "--json"]) == 0
    output = capsys.readouterr().out
    assert "unit-test-openai-key" not in output
    report = json.loads(output)
    assert report["llm"]["provider"] == "openai"
    assert report["llm"]["model"] == "gpt-test"
    assert report["llm"]["api_key_present"] is True


def test_tools_list_outputs_builtin_tools(capsys):
    assert main(["tools", "list", "--category", "generic"]) == 0
    output = capsys.readouterr().out
    assert "generic\tfile" in output
    assert "generic\tstrings" in output


def test_tools_list_json_query(capsys):
    assert main(["tools", "list", "--query", "debugger", "--json"]) == 0
    tools = json.loads(capsys.readouterr().out)
    assert tools[0]["name"] == "gdb"


def test_tools_doctor_does_not_fail_when_tools_missing(capsys):
    assert main(["tools", "doctor", "--category", "forensics"]) == 0
    output = capsys.readouterr().out
    assert "CTF Agent Tools Doctor" in output
    assert "install:" in output


def test_list_local_examples(capsys):
    examples = Path(__file__).resolve().parents[1] / "examples"
    assert main(["list", str(examples)]) == 0
    output = capsys.readouterr().out
    assert "challenge1" in output
    assert "Example Challenge 1" in output


def test_inspect_local_example(capsys):
    challenge = Path(__file__).resolve().parents[1] / "examples" / "challenge1"
    assert main(["inspect", str(challenge)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["id"] == "challenge1"
    assert data["files"] == ["prompt.txt"]


def test_exec_local_example_writes_output(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("CTF_AGENT_WORKSPACE_DIR", str(tmp_path / "workspace"))
    challenge = Path(__file__).resolve().parents[1] / "examples" / "challenge1"
    assert main(["exec", str(challenge), "--executor", "local", "--", "cat ./prompt.txt"]) == 0
    output = capsys.readouterr().out
    assert "local platform adapter example" in output
    run_dir = tmp_path / "workspace" / "runs" / "challenge1"
    assert (run_dir / "state.json").exists()
    assert (run_dir / "trace.jsonl").exists()


def test_exec_default_docker_falls_back_to_local_when_unavailable(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("CTF_AGENT_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr("ctf_agent.cli.app.docker_available", lambda: False)
    challenge = Path(__file__).resolve().parents[1] / "examples" / "challenge1"
    assert main(["exec", str(challenge), "--", "cat ./prompt.txt"]) == 0
    captured = capsys.readouterr()
    assert "falling back to local executor" in captured.err
    assert "local platform adapter example" in captured.out


def test_exec_explicit_docker_fails_cleanly_when_unavailable(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("CTF_AGENT_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr("ctf_agent.cli.app.docker_available", lambda: False)
    challenge = Path(__file__).resolve().parents[1] / "examples" / "challenge1"
    assert main(["exec", str(challenge), "--executor", "docker", "--", "cat ./prompt.txt"]) == 69
    assert "Docker is not available" in capsys.readouterr().err


def test_solve_local_example_finds_flag(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("CTF_AGENT_WORKSPACE_DIR", str(tmp_path / "workspace"))
    challenge = Path(__file__).resolve().parents[1] / "examples" / "challenge1"
    assert main(["solve", str(challenge), "--brain", "fallback", "--executor", "local", "--max-steps", "10"]) == 0
    output = capsys.readouterr().out
    assert "state: solved" in output
    assert "flag: flag{example_only}" in output


def test_solve_specialist_mode_finds_flag(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("CTF_AGENT_WORKSPACE_DIR", str(tmp_path / "workspace"))
    challenge = Path(__file__).resolve().parents[1] / "examples" / "challenge1"
    assert main(["solve", str(challenge), "--brain", "fallback", "--executor", "local", "--mode", "specialist", "--max-steps", "10"]) == 0
    output = capsys.readouterr().out
    assert "state: solved" in output
    assert "flag: flag{example_only}" in output


def test_resume_local_example_finds_saved_flag(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("CTF_AGENT_WORKSPACE_DIR", str(tmp_path / "workspace"))
    challenge = Path(__file__).resolve().parents[1] / "examples" / "challenge1"
    assert main(["solve", str(challenge), "--brain", "fallback", "--executor", "local", "--max-steps", "10"]) == 0
    capsys.readouterr()
    run_dir = tmp_path / "workspace" / "runs" / "challenge1"
    assert main(["resume", str(run_dir), "--brain", "fallback", "--executor", "local", "--max-steps", "10"]) == 0
    output = capsys.readouterr().out
    assert "state: solved" in output
    assert "flag: flag{example_only}" in output


def test_report_cli_generates_writeup(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("CTF_AGENT_WORKSPACE_DIR", str(tmp_path / "workspace"))
    challenge = Path(__file__).resolve().parents[1] / "examples" / "challenge1"
    assert main(["solve", str(challenge), "--brain", "fallback", "--executor", "local", "--max-steps", "10"]) == 0
    capsys.readouterr()
    run_dir = tmp_path / "workspace" / "runs" / "challenge1"
    assert main(["report", str(run_dir)]) == 0
    output = capsys.readouterr().out
    assert "writeup:" in output
    assert (run_dir / "writeup.md").exists()


def test_submit_cli_dry_run(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("CTF_AGENT_WORKSPACE_DIR", str(tmp_path / "workspace"))
    challenge = Path(__file__).resolve().parents[1] / "examples" / "challenge1"
    assert main(["solve", str(challenge), "--brain", "fallback", "--executor", "local", "--max-steps", "10"]) == 0
    capsys.readouterr()
    run_dir = tmp_path / "workspace" / "runs" / "challenge1"
    assert main(["submit", str(run_dir), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "dry_run: True" in output
    assert "submitted: False" in output
    assert "flag: flag{example_only}" in output


def test_docker_doctor_cli_reports_profiles(capsys, monkeypatch):
    monkeypatch.setattr(
        "ctf_agent.cli.app.docker_profiles_doctor",
        lambda run_tools=False, include_optional=False: {
            "ok": True,
            "docker_available": True,
            "run_tools": run_tools,
            "profiles": [
                {
                    "profile": "generic",
                    "image": "ctf-agent:generic",
                    "dockerfile": "docker/Dockerfile.generic",
                    "exists": True,
                    "ok": True,
                    "checks": [],
                    "notes": [],
                }
            ],
        },
    )
    assert main(["docker", "doctor"]) == 0
    output = capsys.readouterr().out
    assert "CTF Agent Docker Profiles Doctor" in output
    assert "ctf-agent:generic" in output


def test_default_solve_graph_without_provider_fails_without_fallback(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv('CTF_AGENT_WORKSPACE_DIR', str(tmp_path / 'workspace'))
    monkeypatch.delenv('CTF_AGENT_LLM_PROVIDER', raising=False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    monkeypatch.delenv('OPENAI_BASE_URL', raising=False)
    challenge = Path(__file__).resolve().parents[1] / 'examples' / 'challenge1'

    assert main(['solve', str(challenge), '--executor', 'local', '--max-steps', '10']) == 1

    captured = capsys.readouterr()
    assert 'state: failed' in captured.out
    assert 'graph mode requires a configured PydanticAI provider' in captured.err
    assert 'ctf-agent doctor llm' in captured.err
    assert '--brain fallback' in captured.err
    assert 'flag{example_only}' not in captured.out


def test_default_eval_graph_without_provider_fails_clearly(capsys, tmp_path, monkeypatch):
    dataset = tmp_path / 'dataset'
    challenge = dataset / 'demo'
    challenge.mkdir(parents=True)
    (challenge / 'challenge.yaml').write_text(
        'id: demo\ntitle: demo\ncategory: misc\nfiles:\n  - flag.txt\nflag_regex: flag\\{[A-Za-z0-9_]+\\}\nmetadata:\n  expected_flag: flag{demo}\n',
        encoding='utf-8',
    )
    (challenge / 'flag.txt').write_text('flag{demo}\n', encoding='utf-8')
    monkeypatch.setenv('CTF_AGENT_WORKSPACE_DIR', str(tmp_path / 'workspace'))
    monkeypatch.setenv('CTF_AGENT_MEMORY_ENABLED', 'false')
    monkeypatch.delenv('CTF_AGENT_LLM_PROVIDER', raising=False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    monkeypatch.delenv('OPENAI_BASE_URL', raising=False)

    assert main(['eval', str(dataset), '--executor', 'local', '--max-steps', '5', '--output-dir', str(tmp_path / 'eval-output')]) == 1

    captured = capsys.readouterr()
    assert 'solved_count: 0/1' in captured.out
    assert 'graph eval failure for demo' in captured.err
    assert 'graph mode requires a configured PydanticAI provider' in captured.err


def test_eval_fallback_explicit_runs_local_plumbing(capsys, tmp_path, monkeypatch):
    dataset = tmp_path / 'dataset'
    challenge = dataset / 'demo'
    challenge.mkdir(parents=True)
    (challenge / 'challenge.yaml').write_text(
        'id: demo\ntitle: demo\ncategory: misc\nfiles:\n  - flag.txt\nflag_regex: flag\\{[A-Za-z0-9_]+\\}\nmetadata:\n  expected_flag: flag{demo}\n',
        encoding='utf-8',
    )
    (challenge / 'flag.txt').write_text('flag{demo}\n', encoding='utf-8')
    monkeypatch.setenv('CTF_AGENT_WORKSPACE_DIR', str(tmp_path / 'workspace'))
    monkeypatch.setenv('CTF_AGENT_MEMORY_ENABLED', 'false')

    assert main(['eval', str(dataset), '--brain', 'fallback', '--executor', 'local', '--max-steps', '5', '--output-dir', str(tmp_path / 'eval-output')]) == 0

    assert 'solved_count: 1/1' in capsys.readouterr().out


@pytest.mark.parametrize('brain', ['llm', 'hybrid'])
def test_legacy_brain_modes_print_deprecation(capsys, tmp_path, monkeypatch, brain):
    monkeypatch.setenv('CTF_AGENT_WORKSPACE_DIR', str(tmp_path / 'workspace'))
    monkeypatch.setenv('CTF_AGENT_MEMORY_ENABLED', 'false')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    challenge = Path(__file__).resolve().parents[1] / 'examples' / 'challenge1'

    main(['solve', str(challenge), '--brain', brain, '--executor', 'local', '--max-steps', '1'])

    assert f'--brain {brain} is deprecated legacy compatibility' in capsys.readouterr().err
