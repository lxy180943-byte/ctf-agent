from pathlib import Path

import pytest

from ctf_agent.agents import AgentContext
from ctf_agent.core.models import Challenge
from ctf_agent.core.orchestrator import Orchestrator
from ctf_agent.sandbox import ExecutionResult, Executor


class StopAfterContext(Exception):
    pass


class RecordingExecutor(Executor):
    def __init__(self) -> None:
        self.calls = 0

    def run(self, command: str, cwd: str | Path, timeout: int | None = None, env: dict[str, str] | None = None) -> ExecutionResult:
        self.calls += 1
        raise AssertionError('executor must not be called while checking resume intent')


def _capture_resume_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    brain: str,
    resume: bool,
) -> tuple[dict[str, object], dict[str, int]]:
    captured: dict[str, object] = {}
    calls = {'checkpoint': 0, 'graph_invoke': 0, 'llm_loop': 0}
    executor = RecordingExecutor()

    import ctf_agent.core.orchestrator as orchestrator_module

    def fake_build_executor(self, challenge, trace_store):
        return executor

    def capture_classify(self, context: AgentContext):
        captured.update(context.metadata)
        raise StopAfterContext

    def forbidden_open_checkpointer(*args, **kwargs):
        calls['checkpoint'] += 1
        raise AssertionError('checkpoint must not be opened while checking resume intent')

    def forbidden_graph_invoke(self, context):
        calls['graph_invoke'] += 1
        raise AssertionError('graph must not be invoked while checking resume intent')

    class ForbiddenLLMActionLoop:
        def __init__(self, *args, **kwargs) -> None:
            calls['llm_loop'] += 1
            raise AssertionError('LLMActionLoop must not be constructed while checking resume intent')

    monkeypatch.setattr(Orchestrator, '_build_executor', fake_build_executor)
    monkeypatch.setattr(Orchestrator, '_classify', capture_classify)
    monkeypatch.setattr(Orchestrator, '_invoke_graph_workflow', forbidden_graph_invoke)
    monkeypatch.setattr(orchestrator_module, 'open_run_checkpointer', forbidden_open_checkpointer)
    monkeypatch.setattr(orchestrator_module, 'LLMActionLoop', ForbiddenLLMActionLoop)

    graph_reasoner = object() if brain == 'graph' else None
    orchestrator = Orchestrator(
        {'workspace_dir': str(tmp_path / 'workspace')},
        brain=brain,
        graph_reasoner=graph_reasoner,
    )
    challenge = Challenge(id=f'resume-intent-{brain}-{resume}', title='Resume Intent', category='misc')
    state = orchestrator.workspace.init_state(challenge)
    layout = orchestrator.workspace.layout_for(challenge.id)

    with pytest.raises(StopAfterContext):
        orchestrator._run_loop(state, layout, resume=resume)

    assert executor.calls == 0
    return captured, calls


@pytest.mark.parametrize(
    ('resume', 'expected_graph_resume'),
    [
        (False, False),
        (True, True),
    ],
)
def test_graph_resume_intent_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resume: bool, expected_graph_resume: bool):
    metadata, calls = _capture_resume_metadata(tmp_path, monkeypatch, brain='graph', resume=resume)

    assert metadata['resume_requested'] is resume
    assert metadata['graph_resume_requested'] is expected_graph_resume
    assert calls == {'checkpoint': 0, 'graph_invoke': 0, 'llm_loop': 0}


def test_fallback_resume_intent_metadata_is_not_graph_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    metadata, calls = _capture_resume_metadata(tmp_path, monkeypatch, brain='fallback', resume=True)

    assert metadata['resume_requested'] is True
    assert metadata['graph_resume_requested'] is False
    assert calls == {'checkpoint': 0, 'graph_invoke': 0, 'llm_loop': 0}
