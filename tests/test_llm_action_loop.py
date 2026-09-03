import json
from pathlib import Path

import pytest

from ctf_agent.agents import AgentContext
from ctf_agent.core.models import Challenge
from ctf_agent.core.state import ChallengeState
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.llm import DummyProvider, PromptStore
from ctf_agent.llm.actions import ActionValidationError, ActionType, parse_action_decision, parse_strict_json_object
from ctf_agent.llm.loop import LLMActionLoop
from ctf_agent.llm.risk import RiskLevel, classify_command_risk
from ctf_agent.sandbox import LocalExecutor
from ctf_agent.tools import default_registry


def _context(tmp_path: Path, provider: DummyProvider, *, content: str = "nothing here\n", max_steps: int = 3) -> AgentContext:
    challenge = Challenge(id="toy-loop", title="Toy Loop", category="misc", files=["prompt.txt"])
    manager = WorkspaceManager(tmp_path / "workspace")
    state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    (layout.work_dir / "prompt.txt").write_text(content, encoding="utf-8")
    return AgentContext(
        state=state,
        layout=layout,
        trace_store=manager.trace_store_for(challenge.id),
        executor=LocalExecutor(tmp_path / "workspace", trace_store=manager.trace_store_for(challenge.id), challenge_id=challenge.id),
        tool_registry=default_registry(),
        config={},
        max_steps=max_steps,
        timeout=10,
        llm_provider=provider,
        prompt_store=PromptStore(Path(__file__).resolve().parents[1] / "prompts"),
    )


def test_strict_json_rejects_embedded_text():
    with pytest.raises(ActionValidationError):
        parse_strict_json_object('before {"rationale":"x","actions":[]} after')


def test_action_schema_rejects_unknown_action():
    with pytest.raises(ActionValidationError):
        parse_action_decision('{"rationale":"x","actions":[{"type":"teleport"}]}')


def test_action_schema_limits_actions_to_three():
    payload = {"rationale": "too many", "actions": [{"type": "pause"} for _ in range(4)]}
    with pytest.raises(ActionValidationError):
        parse_action_decision(json.dumps(payload))


def test_action_schema_parses_valid_run_command():
    decision = parse_action_decision(
        '{"rationale":"inspect","actions":[{"type":"run_command","command":"file ./prompt.txt","reason":"identify","timeout":5}]}'
    )
    assert decision.actions[0].type is ActionType.RUN_COMMAND
    assert decision.actions[0].command == "file ./prompt.txt"


def test_action_schema_parses_brain_prompt_shape():
    decision = parse_action_decision(
        '{"hypothesis":"source leak","evidence_used":["highlight_file output"],"uncertainty":["need to confirm include path"],"next_actions":[{"type":"read_file","path":"prompt.txt","reason":"inspect"}]}'
    )
    assert decision.hypothesis == "source leak"
    assert decision.evidence_used == ["highlight_file output"]
    assert decision.uncertainty == ["need to confirm include path"]
    assert decision.next_actions[0].type is ActionType.READ_FILE


def test_dummy_provider_illegal_json_is_observed_and_rejected(tmp_path):
    context = _context(tmp_path, DummyProvider(["not json"]), max_steps=1)
    result = LLMActionLoop(save_state=lambda state: context.layout.state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")).run(context)
    assert result.solved is False
    assert result.observations[0].source == "llm-guard"
    assert "action-validation-failed" in context.layout.trace_path.read_text(encoding="utf-8")


def test_dummy_provider_unknown_action_is_observed_and_rejected(tmp_path):
    context = _context(tmp_path, DummyProvider(['{"rationale":"bad","actions":[{"type":"teleport"}]}']), max_steps=1)
    result = LLMActionLoop().run(context)
    assert result.solved is False
    assert "unknown type" in result.observations[0].summary


def test_dummy_provider_out_of_bounds_read_is_guarded(tmp_path):
    context = _context(tmp_path, DummyProvider(['{"rationale":"escape","actions":[{"type":"read_file","path":"../outside.txt"}]}']), max_steps=1)
    result = LLMActionLoop().run(context)
    assert result.solved is False
    assert result.observations[0].source == "llm-guard"
    assert context.state.state is ChallengeState.FAILED


def test_dummy_provider_fake_finish_flag_is_guarded(tmp_path):
    context = _context(tmp_path, DummyProvider(['{"rationale":"fake","actions":[{"type":"finish","flag":"flag{fabricated}"}]}']), max_steps=1)
    result = LLMActionLoop().run(context)
    assert result.solved is False
    assert "finish flag was not present" in result.observations[0].summary
    assert context.state.flag_candidates == []


def test_high_risk_command_requires_confirmation_and_is_not_executed(tmp_path):
    context = _context(tmp_path, DummyProvider(['{"rationale":"bad","actions":[{"type":"run_command","command":"rm -rf ./prompt.txt"}]}']), max_steps=1)
    result = LLMActionLoop().run(context)
    assert result.solved is False
    assert result.observations[0].source == "risk-classifier"
    assert result.observations[0].metadata["confirm_required"] is True
    assert (context.layout.work_dir / "prompt.txt").exists()


def test_command_risk_classifier_scopes_network_commands():
    assert classify_command_risk("sudo id").level is RiskLevel.REFUSE
    assert classify_command_risk("curl http://example.com").confirm_required is True
    scoped = classify_command_risk("curl http://ctf.local:8080/", "http://ctf.local:8080")
    assert scoped.level is RiskLevel.MEDIUM


def test_valid_interactive_loop_reads_and_verifies_flag(tmp_path):
    provider = DummyProvider(
        [
            '{"rationale":"read and verify","actions":[{"type":"read_file","path":"prompt.txt","reason":"inspect prompt"},{"type":"ask_verifier","reason":"extract flag"}]}'
        ]
    )
    context = _context(tmp_path, provider, content="hello flag{tool_loop}\n", max_steps=3)
    result = LLMActionLoop().run(context)
    assert result.solved is True
    assert [candidate.value for candidate in context.state.flag_candidates] == ["flag{tool_loop}"]
    assert context.state.state is ChallengeState.SOLVED



def test_loop_adds_php_analysis_to_next_prompt(tmp_path):
    php = "<?php highlight_file(__FILE__); if (md5($_GET['token']) == '0e1') { include($_GET['page'] . '.php'); }"
    provider = DummyProvider(
        [
            '{"rationale":"read source","actions":[{"type":"read_file","path":"prompt.txt","reason":"inspect php"}]}',
            '{"rationale":"enough","actions":[{"type":"pause","reason":"test captured prompt"}]}',
        ]
    )
    context = _context(tmp_path, provider, content=php, max_steps=2)
    result = LLMActionLoop().run(context)
    assert result.paused is True
    assert context.metadata['php_analysis']
    second_prompt = provider.calls[1][1].content
    assert 'PHP analysis JSON' in second_prompt
    assert 'type-juggling-plus-lfi-chain' in second_prompt
