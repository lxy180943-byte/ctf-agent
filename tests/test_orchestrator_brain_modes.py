import pytest

from ctf_agent.core.orchestrator import Orchestrator


@pytest.mark.parametrize("brain", ["llm", "fallback", "hybrid", "graph"])
def test_supported_brain_modes_construct(brain: str, tmp_path):
    orchestrator = Orchestrator({"workspace_dir": str(tmp_path)}, brain=brain)
    assert orchestrator.brain == brain


def test_invalid_brain_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unknown brain mode: invalid"):
        Orchestrator({"workspace_dir": str(tmp_path)}, brain="invalid")


def test_default_brain_is_graph(tmp_path):
    orchestrator = Orchestrator({'workspace_dir': str(tmp_path)})
    assert orchestrator.brain == 'graph'


def test_default_graph_missing_provider_does_not_call_fallback_or_llm_loop(tmp_path, monkeypatch):
    from ctf_agent.core.models import Challenge

    calls = {'initial_plan': 0, 'llm_loop': 0}

    def forbidden_initial_plan(*args, **kwargs):
        calls['initial_plan'] += 1
        raise AssertionError('graph provider failure must not enter deterministic fallback')

    class ForbiddenLLMActionLoop:
        def __init__(self, *args, **kwargs):
            calls['llm_loop'] += 1
            raise AssertionError('graph provider failure must not enter LLMActionLoop')

    import ctf_agent.core.orchestrator as orchestrator_module

    monkeypatch.delenv('CTF_AGENT_LLM_PROVIDER', raising=False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    monkeypatch.delenv('OPENAI_BASE_URL', raising=False)
    monkeypatch.setattr(Orchestrator, '_initial_plan', forbidden_initial_plan)
    monkeypatch.setattr(orchestrator_module, 'LLMActionLoop', ForbiddenLLMActionLoop)

    orchestrator = Orchestrator({'workspace_dir': str(tmp_path / 'workspace'), 'sandbox': {'engine': 'local'}})
    result = orchestrator.solve(Challenge(id='default-graph-missing-provider', title='Default Graph', category='misc'))

    assert result.state.value == 'failed'
    assert 'graph mode requires a configured PydanticAI provider' in result.metadata['graph_failure_reason']
    assert 'ctf-agent doctor llm' in result.metadata['graph_failure_reason']
    assert '--brain fallback' in result.metadata['graph_failure_reason']
    assert calls == {'initial_plan': 0, 'llm_loop': 0}
