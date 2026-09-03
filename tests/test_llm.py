import json
from pathlib import Path

import pytest

from ctf_agent.agents import AgentContext, ExecutionBatch, PlannerAgent, VerifierAgent
from ctf_agent.core.config import ConfigError, apply_env_overrides, load_config
from ctf_agent.core.models import Artifact, Challenge
from ctf_agent.core.orchestrator import Orchestrator
from ctf_agent.core.state import ChallengeRunState
from ctf_agent.core.trace import TraceStore
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.llm import DummyProvider, LLMMessage, OpenAICompatibleProvider, PromptStore, build_provider, render_template
from ctf_agent.llm.actions import extract_command_actions, parse_json_object
from ctf_agent.platforms.local import LocalPlatformAdapter
from ctf_agent.sandbox import ExecutionResult, LocalExecutor
from ctf_agent.tools import default_registry


def test_render_template_replaces_values():
    assert render_template("hello {{ name }}", {"name": "planner"}) == "hello planner"


def test_render_template_requires_known_values():
    with pytest.raises(KeyError):
        render_template("hello {{ missing }}", {})


def test_prompt_store_loads_planner_prompt():
    prompt = PromptStore(Path(__file__).resolve().parents[1] / "prompts").render(
        "planner",
        {
            "challenge_json": "{}",
            "tools_json": "[]",
            "memory_json": "[]",
            "observed_paths_json": "[]",
            "observations_json": "[]",
            "php_analysis_json": "[]",
            "trace_json": "[]",
            "flag_candidates_json": "[]",
            "brain_context_json": "{}",
        },
    )
    assert "Return strict JSON only" in prompt
    assert "Do not fabricate files" in prompt


def test_dummy_provider_records_calls():
    provider = DummyProvider(['{"commands":[]}'])
    response = provider.complete([LLMMessage(role="user", content="hi")])
    assert response.provider == "dummy"
    assert json.loads(response.content) == {"commands": []}
    assert provider.calls[0][0].content == "hi"


def test_build_provider_from_env_openai_compatible():
    provider = build_provider(
        {"llm": {"provider": "none"}},
        environ={
            "CTF_AGENT_LLM_PROVIDER": "openai-compatible",
            "OPENAI_BASE_URL": "https://llm.example/v1",
            "OPENAI_API_KEY": "unit-test-openai-key",
            "OPENAI_MODEL": "ctf-model",
        },
    )
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == "https://llm.example/v1"
    assert provider.api_key == "unit-test-openai-key"
    assert provider.model == "ctf-model"


def test_openai_compatible_provider_uses_chat_completions(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": '{"commands":[]}'}}]}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider("https://llm.example/v1", "secret", "ctf-model", timeout=7)
    response = provider.complete([LLMMessage(role="user", content="plan")])
    assert response.content == '{"commands":[]}'
    assert captured["url"] == "https://llm.example/v1/chat/completions"
    assert captured["payload"]["model"] == "ctf-model"
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["timeout"] == 7


def test_parse_json_object_extracts_embedded_object():
    assert parse_json_object("text {\"commands\": []} tail") == {"commands": []}


def test_extract_command_actions_limits_and_filters():
    data = {
        "commands": [
            {"command": "file ./a", "reason": "inspect", "timeout": 3},
            {"command": "", "reason": "skip"},
            {"command": "cat ./b"},
            {"command": "cat ./c"},
        ]
    }
    actions = extract_command_actions(data, max_actions=3)
    assert [action["command"] for action in actions] == ["file ./a", "cat ./b"]


def test_planner_uses_dummy_provider_for_json_plan(tmp_path):
    challenge = Challenge(id="toy", title="Toy", category="misc", files=["prompt.txt"])
    manager = WorkspaceManager(tmp_path / "workspace")
    state = manager.init_state(challenge)
    provider = DummyProvider(['{"rationale":"use cat","commands":[{"command":"cat ./prompt.txt","reason":"read prompt","timeout":5}]}'])
    context = AgentContext(
        state=state,
        layout=manager.layout_for(challenge.id),
        trace_store=manager.trace_store_for(challenge.id),
        executor=LocalExecutor(tmp_path / "workspace"),
        tool_registry=default_registry(),
        config={},
        max_steps=10,
        timeout=30,
        llm_provider=provider,
        prompt_store=PromptStore(Path(__file__).resolve().parents[1] / "prompts"),
    )
    plan = PlannerAgent().run(context)
    assert plan.metadata["source"] == "llm"
    assert plan.commands[0].command == "cat ./prompt.txt"


def test_verifier_rejects_llm_candidate_not_in_observations(tmp_path):
    challenge = Challenge(id="toy", title="Toy", category="misc", flag_regex=r"NO_MATCH")
    manager = WorkspaceManager(tmp_path / "workspace")
    state = manager.init_state(challenge)
    provider = DummyProvider(['{"candidates":[{"value":"flag{fabricated}","source":"llm","confidence":1.0,"verified":true}]}'])
    batch = ExecutionBatch(
        results=[
            ExecutionResult(
                command="cat prompt.txt",
                cwd=str(tmp_path),
                env={},
                timeout=10,
                exit_code=0,
                stdout="there is no candidate here",
                stderr="",
                started_at="2026-08-31T00:00:00Z",
                ended_at="2026-08-31T00:00:01Z",
                duration_seconds=1.0,
            )
        ]
    )
    context = AgentContext(
        state=state,
        layout=manager.layout_for(challenge.id),
        trace_store=manager.trace_store_for(challenge.id),
        executor=LocalExecutor(tmp_path / "workspace"),
        tool_registry=default_registry(),
        config={},
        max_steps=10,
        timeout=30,
        llm_provider=provider,
        prompt_store=PromptStore(Path(__file__).resolve().parents[1] / "prompts"),
        metadata={"execution_batch": batch},
    )
    result = VerifierAgent().run(context)
    assert result.candidates == []


def test_orchestrator_uses_dummy_provider_but_executes_through_executor(tmp_path):
    challenge_dir = tmp_path / "challenge1"
    challenge_dir.mkdir()
    (challenge_dir / "challenge.yaml").write_text("title: Toy\ncategory: misc\nfiles:\n  - prompt.txt\n", encoding="utf-8")
    (challenge_dir / "prompt.txt").write_text("flag{llm_mvp}\n", encoding="utf-8")
    adapter = LocalPlatformAdapter(challenge_dir)
    provider = DummyProvider(['{"rationale":"read prompt","actions":[{"type":"run_command","command":"cat ./prompt.txt","reason":"read prompt","timeout":5},{"type":"ask_verifier","reason":"check observed output"}]}'])
    orchestrator = Orchestrator(
        {"workspace_dir": str(tmp_path / "workspace"), "sandbox": {"engine": "local", "timeout_seconds": 10}},
        executor_name="local",
        llm_provider=provider,
        prompt_store=PromptStore(Path(__file__).resolve().parents[1] / "prompts"),
        brain="llm",
    )
    result = orchestrator.solve(adapter.get_challenge(str(challenge_dir)), adapter=adapter)
    assert result.solved is True
    assert result.flags == ["flag{llm_mvp}"]
    trace = (result.run_dir / "trace.jsonl").read_text(encoding="utf-8")
    assert '"action": "decision"' in trace
    assert '"action": "run-command"' in trace


def test_orchestrator_dummy_llm_does_not_use_deterministic_plan(tmp_path, monkeypatch):
    challenge_dir = tmp_path / "challenge1"
    challenge_dir.mkdir()
    (challenge_dir / "challenge.yaml").write_text(
        "title: Toy\ncategory: misc\nfiles:\n  - prompt.txt\n",
        encoding="utf-8",
    )
    (challenge_dir / "prompt.txt").write_text("flag{llm_primary}\n", encoding="utf-8")
    adapter = LocalPlatformAdapter(challenge_dir)
    provider = DummyProvider([
        '{"rationale":"inspect and verify","actions":[{"type":"read_file","path":"prompt.txt","reason":"inspect"},{"type":"ask_verifier","reason":"verify"}]}'
    ])
    orchestrator = Orchestrator(
        {"workspace_dir": str(tmp_path / "workspace"), "sandbox": {"engine": "local", "timeout_seconds": 10}},
        executor_name="local",
        llm_provider=provider,
        prompt_store=PromptStore(Path(__file__).resolve().parents[1] / "prompts"),
        brain="llm",
    )
    def fail_if_fallback_plan(*_args):
        raise AssertionError("fallback plan used")
    monkeypatch.setattr(orchestrator, "_initial_plan", fail_if_fallback_plan)

    result = orchestrator.solve(adapter.get_challenge(str(challenge_dir)), adapter=adapter)

    assert result.solved is True
    assert result.metadata["brain_mode"] == "llm"
    trace = (result.run_dir / "trace.jsonl").read_text(encoding="utf-8")
    assert '"action": "brain-mode"' in trace
    assert '"brain_mode": "llm"' in trace


def test_llm_provider_env_override_keeps_openai_connection_env_only():
    config = apply_env_overrides(
        {"llm": {"provider": "none", "timeout_seconds": 60}},
        environ={
            "CTF_AGENT_LLM_PROVIDER": "dummy",
            "OPENAI_BASE_URL": "https://llm.example/v1",
            "OPENAI_MODEL": "demo",
        },
    )
    assert config["llm"]["provider"] == "dummy"
    assert "base_url" not in config["llm"]
    assert "model" not in config["llm"]


def test_load_config_rejects_llm_connection_fields_even_when_local(tmp_path):
    config = tmp_path / "profile.local.yaml"
    config.write_text("llm:\n  provider: openai\n  model: demo\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="environment-only"):
        load_config(config, environ={})



def test_build_provider_openai_env_aliases_and_timeout():
    provider = build_provider(
        {'llm': {'provider': 'none'}},
        environ={
            'CTF_AGENT_LLM_PROVIDER': 'openai',
            'OPENAI_API_KEY': 'unit-test-openai-key',
            'OPENAI_MODEL': 'gpt-test',
            'CTF_AGENT_LLM_TIMEOUT': '9',
        },
    )
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == 'https://api.openai.com/v1'
    assert provider.api_key == 'unit-test-openai-key'
    assert provider.timeout == 9


def test_openai_provider_redacts_http_error_body(monkeypatch):
    import io
    import urllib.error

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            'unauthorized',
            hdrs=None,
            fp=io.BytesIO(b'{"error":"bad unit-test-openai-key"}'),
        )

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
    provider = OpenAICompatibleProvider('https://llm.example/v1', 'unit-test-openai-key', 'ctf-model', timeout=7)
    with pytest.raises(RuntimeError) as excinfo:
        provider.complete([LLMMessage(role='user', content='plan')])
    assert 'unit-test-openai-key' not in str(excinfo.value)
    assert '[REDACTED]' in str(excinfo.value)
