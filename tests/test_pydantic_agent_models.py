"""Offline contracts for the PydanticAI reasoning boundary."""

from __future__ import annotations

import pytest

pydantic = pytest.importorskip("pydantic")

from ctf_agent.pydantic_agent.agent import DummySolverModel, SolverDependencies, load_provider_settings
from ctf_agent.pydantic_agent.models import SolverDecision


def decision_payload() -> dict[str, object]:
    return {
        "current_hypothesis": {"name": "source disclosure", "claim": "The observation indicates a local file read primitive may be reachable.", "evidence_for": ["The response contains an include call."], "evidence_against": [], "confidence": 0.55, "falsification_test": "Read a harmless permitted local file through the stated parameter."},
        "confirmed_facts": ["The supplied observation contains PHP source."], "unknowns": ["Whether the include parameter accepts traversal."], "candidate_chains": [["validate read primitive", "inspect permitted source"]],
        "selected_experiment": {"goal": "Validate the suspected read primitive.", "action_type": "read_file", "action_input": {"type": "read_file", "path": "prompt.txt"}, "expected_signal": "The response contains the known harmless file marker.", "failure_signal": "The response rejects the parameter or omits the marker.", "risk": "low", "rollback": "No persistent state is changed."},
        "next_action": "execute_selected_experiment", "need_human": False, "stop_reason": None,
    }


def test_dummy_solver_returns_strict_decision() -> None:
    result = DummySolverModel(decision_payload()).decide(SolverDependencies(challenge={"name": "local toy"}))
    assert isinstance(result, SolverDecision)
    assert result.selected_experiment is not None
    assert result.selected_experiment.action_input.type == "read_file"


@pytest.mark.parametrize(("path", "value"), [(("current_hypothesis", "confidence"), 1.01), (("selected_experiment", "expected_signal"), ""), (("selected_experiment", "failure_signal"), "")])
def test_invalid_reasoning_contract_is_rejected(path, value) -> None:
    payload = decision_payload(); payload[path[0]][path[1]] = value
    with pytest.raises(pydantic.ValidationError): SolverDecision.model_validate(payload)


@pytest.mark.parametrize("claim", ["The answer is flag{must-not-be-returned}.", "I executed the command already and saw the expected marker."])
def test_flag_and_execution_claims_are_rejected(claim: str) -> None:
    payload = decision_payload(); payload["current_hypothesis"]["claim"] = claim
    with pytest.raises(pydantic.ValidationError): SolverDecision.model_validate(payload)


def test_provider_settings_use_only_supplied_environment() -> None:
    openai = load_provider_settings("openai-compatible", environ={"OPENAI_API_KEY": "test-openai-key", "OPENAI_BASE_URL": "https://local.example/v1", "OPENAI_MODEL": "test-model"})
    anthropic = load_provider_settings("anthropic", environ={"ANTHROPIC_API_KEY": "test-anthropic-key", "ANTHROPIC_MODEL": "claude-test"})
    assert openai.provider == "openai-compatible" and "test-openai-key" not in repr(openai)
    assert anthropic.provider == "anthropic" and anthropic.base_url is None


def test_provider_settings_require_the_expected_environment() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        load_provider_settings("openai", environ={"OPENAI_BASE_URL": "https://local.example/v1", "OPENAI_MODEL": "test"})
