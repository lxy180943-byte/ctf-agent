import json
from pathlib import Path

import pytest

from ctf_agent.core.config import ConfigError, load_config
from ctf_agent.core.models import Challenge, FlagCandidate
from ctf_agent.core.state import ChallengeState
from ctf_agent.core.submitter import Submitter
from ctf_agent.core.trace import TraceEvent, TraceStore
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.platforms.ctfd import CTFdPlatformAdapter, DownloadInfo
from ctf_agent.sandbox import DockerExecutor, LocalExecutor
from ctf_agent.sandbox.executor import CommandSafetyError, WorkspaceBoundaryError
from ctf_agent.sandbox.network_policy import docker_network_policy


class SubmitTransport:
    def __init__(self):
        self.posts = []

    def get_json(self, path):
        return {"success": True, "data": []}

    def post_json(self, path, payload):
        self.posts.append((path, payload))
        return {"success": True, "data": {"status": "correct", "message": "correct"}}

    def download(self, url, destination):
        Path(destination).write_text("fixture", encoding="utf-8")
        return DownloadInfo(url=url, size=7, sha256="demo")


def test_local_executor_rejects_cwd_path_traversal(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    executor = LocalExecutor(workspace)
    with pytest.raises(WorkspaceBoundaryError):
        executor.run("true", cwd=outside)


def test_destructive_commands_must_stay_inside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    cwd = workspace / "runs" / "demo" / "work"
    cwd.mkdir(parents=True)
    executor = LocalExecutor(workspace)
    for command in ("rm -rf /", "rm -rf ../../../../outside", "mv ./x /tmp/x", "dd if=./x of=/tmp/out", "rm -rf $HOME/.cache"):
        with pytest.raises(CommandSafetyError):
            executor.run(command, cwd=cwd)


def test_trace_redacts_secret_values_and_sensitive_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-openai-key")
    trace = TraceStore(tmp_path / "trace.jsonl")
    trace.append(
        TraceEvent(
            challenge_id="demo",
            agent="test",
            action="secret-check",
            command=["curl", "-H", "Authorization: Bearer unit-test-openai-key", "https://example.invalid"],
            stdout="token=unit-test-openai-key",
            stderr="Bearer unit-test-openai-key",
            metadata={
                "api_key": "plain-config-secret",
                "headers": {"Authorization": "Token unit-test-openai-key"},
                "safe": "unit-test-openai-key",
            },
        )
    )
    raw = trace.path.read_text(encoding="utf-8")
    assert "unit-test-openai-key" not in raw
    assert "plain-config-secret" not in raw
    assert raw.count("<redacted>") >= 4
    data = json.loads(raw)
    assert data["metadata"]["api_key"] == "<redacted>"


def test_docker_network_requires_explicit_config_and_challenge_authorization():
    challenge = Challenge(id="web1", title="Web", category="web")
    denied = docker_network_policy({"sandbox": {"network": "bridge"}}, challenge)
    assert denied.effective_network == "none"
    assert denied.allowed is False

    allowed = docker_network_policy({"sandbox": {"network": "bridge", "allow_network": True}}, challenge)
    assert allowed.effective_network == "bridge"
    assert allowed.allowed is True
    assert allowed.authorization_source == "challenge.category=web"

    remote = Challenge(id="pwn1", title="Pwn", category="pwn", connection="nc ctf.example 31337")
    allowed_remote = docker_network_policy({"sandbox": {"network": "bridge", "allow_network": True}}, remote)
    assert allowed_remote.effective_network == "bridge"
    assert allowed_remote.authorization_source == "challenge.connection"


def test_docker_executor_defaults_to_network_none(tmp_path):
    executor = DockerExecutor(tmp_path / "workspace", image="ctf-agent:generic")
    command = executor._docker_command("true", Path("/workspace"), timeout=3, env={})
    network_index = command.index("--network")
    assert command[network_index + 1] == "none"


def test_plaintext_secrets_rejected_from_non_local_config(tmp_path):
    config = tmp_path / "default.yaml"
    config.write_text("llm:\n  provider: openai-compatible\n  base_url: https://llm.example/v1\n  api_key: replace-with-key\n  model: demo\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(config, environ={})

    local_config = tmp_path / "profile.local.yaml"
    local_config.write_text(config.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ConfigError, match="environment-only"):
        load_config(local_config, environ={})


def test_ctfd_submit_requires_exact_confirmation_string():
    transport = SubmitTransport()
    adapter = CTFdPlatformAdapter("https://ctf.example", "token", profile_name="demo", submit_enabled=True, transport=transport)
    challenge = Challenge(id="7", title="Warmup", category="misc")

    wrong = adapter.submit_flag(challenge, "flag{demo}", submit=True, confirm="SUBMIT")
    assert wrong.submitted is False
    assert transport.posts == []

    correct = adapter.submit_flag(challenge, "flag{demo}", submit=True, confirm="SUBMIT demo 7")
    assert correct.submitted is True
    assert transport.posts == [("/api/v1/challenges/attempt", {"challenge_id": 7, "submission": "flag{demo}"})]


def test_generic_submitter_cannot_bypass_ctfd_confirmation(tmp_path):
    workspace = tmp_path / "workspace"
    manager = WorkspaceManager(workspace)
    challenge = Challenge(id="7", title="Warmup", category="misc", metadata={"source": "ctfd", "profile": "demo"})
    state = manager.init_state(challenge)
    state.state = ChallengeState.SOLVED
    state.add_flag_candidate(FlagCandidate(value="flag{demo}", source="test", confidence=1.0, verified=True))
    manager.save_state(state)
    result = Submitter(
        {
            "workspace_dir": str(workspace),
            "platform": {"ctfd": {"profiles": {"demo": {"url": "https://ctf.example", "token": "token", "submit_enabled": True}}}},
        }
    ).submit_run(manager.layout_for("7").challenge_dir, submit=True)
    assert result.submitted is False
    assert "confirmation string" in result.message
