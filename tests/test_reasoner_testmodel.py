import asyncio
import json
import pytest
from ctf_agent.pydantic_agent.agent import PydanticAISolverReasoner, ReasoningError, SolverDependencies
from ctf_agent.pydantic_agent.models import SolverDecision

def _fixture():
    return {"current_hypothesis":{"name":"local source","claim":"A supplied local source file should be inspected.","evidence_for":["challenge files are present"],"evidence_against":[],"confidence":0.5,"falsification_test":"Read the named local source file."},"confirmed_facts":["This is an authorized local toy."],"unknowns":["source contents"],"candidate_chains":[["read source","summarize evidence"]],"selected_experiment":{"goal":"Read the local source.","action_type":"read_file","action_input":{"type":"read_file","path":"work/source.php"},"expected_signal":"PHP source is returned.","failure_signal":"The file cannot be read.","risk":"low","rollback":"No state changes."},"next_action":"execute_selected_experiment","need_human":False,"stop_reason":None}

def test_testmodel_runs_sync_with_deps_and_returns_solver_decision():
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    try:
        reasoner=PydanticAISolverReasoner.test_model(_fixture())
        secret="unit-secret-must-not-leak"
        deps=SolverDependencies(challenge={"id":"toy","title":"Toy","category":"misc"},graph_state_snapshot={"iteration":0},recent_observations=[{"source":"local"}],run_id="toy",provider_name="test",model_name="TestModel")
        result=reasoner.reason({"iteration":0},deps)
        assert isinstance(result,SolverDecision)
        assert result.selected_experiment.action_input.type=="read_file"
        assert result.selected_experiment.action_input.path=="work/source.php"
        serialized=json.dumps(deps.__dict__,sort_keys=True)
        for forbidden in ("OPENAI_API_KEY","ANTHROPIC_API_KEY","Authorization","Bearer",secret): assert forbidden not in serialized
        assert reasoner.agent.model.last_model_request_parameters is not None
    finally:
        loop.close(); asyncio.set_event_loop(None)

def test_missing_provider_is_redacted():
    secret="sk-secret-should-not-appear"
    with pytest.raises(ReasoningError) as exc: PydanticAISolverReasoner(provider="openai",environ={"OPENAI_API_KEY":secret})
    assert secret not in str(exc.value)
    assert "key" not in str(exc.value).lower()
