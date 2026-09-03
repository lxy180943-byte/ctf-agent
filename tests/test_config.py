from pathlib import Path

from ctf_agent.core.config import apply_env_overrides, load_config, parse_simple_yaml


def test_parse_simple_yaml_nested_values():
    config = parse_simple_yaml(
        """
workspace_dir: ~/ctf-workspace
logging:
  level: DEBUG
  trace_enabled: true
sandbox:
  timeout_seconds: 30
  cpu: 1.5
"""
    )
    assert config["logging"]["level"] == "DEBUG"
    assert config["logging"]["trace_enabled"] is True
    assert config["sandbox"]["timeout_seconds"] == 30
    assert config["sandbox"]["cpu"] == 1.5


def test_parse_simple_yaml_lists_and_empty_values():
    config = parse_simple_yaml(
        """
files:
  - chall
  - notes.txt
connection:
"""
    )
    assert config["files"] == ["chall", "notes.txt"]
    assert config["connection"] is None


def test_parse_simple_yaml_keeps_none_as_string():
    config = parse_simple_yaml("sandbox:\n  network: none\n")
    assert config["sandbox"]["network"] == "none"


def test_env_overrides_known_keys():
    config = {"logging": {"level": "INFO"}, "submit": {"enabled": False}}
    merged = apply_env_overrides(
        config,
        environ={
            "CTF_AGENT_LOG_LEVEL": "DEBUG",
            "CTF_AGENT_SUBMIT_ENABLED": "true",
        },
    )
    assert merged["logging"]["level"] == "DEBUG"
    assert merged["submit"]["enabled"] is True


def test_load_config_expands_paths(tmp_path):
    config_path = tmp_path / "default.yaml"
    config_path.write_text(
        "workspace_dir: ~/ctf-workspace\nartifacts_dir: ~/ctf-artifacts\nlogging:\n  trace_path: ~/ctf-workspace/t.jsonl\n",
        encoding="utf-8",
    )
    config = load_config(config_path, environ={})
    assert config["workspace_dir"].startswith(str(Path.home()))
    assert config["logging"]["trace_path"].startswith(str(Path.home()))


def test_load_config_ignores_empty_config_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "default.yaml").write_text(
        "workspace_dir: ~/ctf-workspace\nartifacts_dir: ~/ctf-artifacts\nlogging:\n  trace_path: ~/trace.jsonl\n",
        encoding="utf-8",
    )
    config = load_config(environ={"CTF_AGENT_CONFIG": ""})
    assert config["artifacts_dir"].startswith(str(Path.home()))
