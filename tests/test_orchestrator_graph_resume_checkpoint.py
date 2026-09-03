import asyncio
from contextlib import contextmanager
from pathlib import Path

import pytest

from ctf_agent.agents import AgentContext
from ctf_agent.core.models import Challenge
from ctf_agent.core.orchestrator import Orchestrator
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.graph import nodes as graph_nodes
from ctf_agent.graph.checkpoint import CHECKPOINT_RELATIVE_PATH, graph_thread_id
from ctf_agent.pydantic_agent.agent import PydanticAISolverReasoner, SolverDependencies
from ctf_agent.pydantic_agent.models import SolverDecision
from ctf_agent.sandbox import ExecutionResult, Executor
from ctf_agent.tools import default_registry


class RecordingExecutor(Executor):
    def __init__(self) -> None:
        self.calls = 0

    def run(self, command: str, cwd: str | Path, timeout: int | None = None, env: dict[str, str] | None = None) -> ExecutionResult:
        self.calls += 1
        raise AssertionError('executor must not be called by graph resume checkpoint tests')


def _pause_decision() -> dict[str, object]:
    return {
        'current_hypothesis': {
            'name': 'pause',
            'claim': 'Human input is required before execution.',
            'evidence_for': [],
            'evidence_against': [],
            'confidence': 0.4,
            'falsification_test': 'Ask for the missing scope.',
        },
        'confirmed_facts': [],
        'unknowns': ['scope'],
        'candidate_chains': [],
        'selected_experiment': {
            'goal': 'Pause for human scope confirmation.',
            'action_type': 'pause',
            'action_input': {'type': 'pause', 'reason': 'Need human scope confirmation.'},
            'expected_signal': 'human',
            'failure_signal': 'no input',
            'risk': 'low',
            'rollback': 'none',
        },
        'next_action': 'execute_selected_experiment',
        'need_human': False,
        'stop_reason': None,
    }


def _reasoner() -> PydanticAISolverReasoner:
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    return PydanticAISolverReasoner(
        agent=Agent(
            TestModel(custom_output_args=_pause_decision()),
            deps_type=SolverDependencies,
            output_type=SolverDecision,
            instructions='test',
        )
    )


def _context(tmp_path: Path, executor: RecordingExecutor | None = None, challenge_id: str = 'graph-resume-checkpoint') -> AgentContext:
    manager = WorkspaceManager(tmp_path / 'workspace')
    challenge = Challenge(id=challenge_id, title='Graph Resume Checkpoint', category='misc')
    state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    trace = manager.trace_store_for(challenge.id)
    return AgentContext(
        state=state,
        layout=layout,
        trace_store=trace,
        executor=executor or RecordingExecutor(),
        tool_registry=default_registry(),
        config={'workspace_dir': str(tmp_path / 'workspace')},
        max_steps=3,
        timeout=7,
        metadata={'memory_matches': [], 'relevant_skill_notes': []},
    )


@pytest.fixture
def forbid_legacy_paths(monkeypatch: pytest.MonkeyPatch):
    calls = {'llm_loop': 0, 'http': 0}

    class ForbiddenLLMActionLoop:
        def __init__(self, *args, **kwargs) -> None:
            calls['llm_loop'] += 1
            raise AssertionError('LLMActionLoop must not be constructed by graph resume checkpoint tests')

    def forbidden_http(*args, **kwargs):
        calls['http'] += 1
        raise AssertionError('http_request must not be called by pause decision')

    import ctf_agent.core.orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, 'LLMActionLoop', ForbiddenLLMActionLoop)
    monkeypatch.setattr(graph_nodes, 'http_request', forbidden_http)
    return calls


def test_resume_uses_existing_checkpoint_without_initial_workflow_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forbid_legacy_paths):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    captured = {'opened': [], 'closed': [], 'invocations': [], 'checkpointers': []}
    executor = RecordingExecutor()

    import ctf_agent.core.orchestrator as orchestrator_module

    real_open_run_checkpointer = orchestrator_module.open_run_checkpointer
    real_build_workflow = orchestrator_module.build_workflow

    @contextmanager
    def tracking_open_run_checkpointer(run_dir: Path):
        captured['opened'].append(Path(run_dir))
        try:
            with real_open_run_checkpointer(run_dir) as checkpointer:
                yield checkpointer
        finally:
            captured['closed'].append(Path(run_dir))

    def tracking_build_workflow(*, checkpointer=None, **kwargs):
        captured['checkpointers'].append(checkpointer)
        workflow = real_build_workflow(checkpointer=checkpointer, **kwargs)

        class TrackingWorkflow:
            def invoke(self, graph_state, config=None, **invoke_kwargs):
                captured['invocations'].append({'graph_state': graph_state, 'config': config, 'kwargs': invoke_kwargs})
                return workflow.invoke(graph_state, config=config, **invoke_kwargs)

        return TrackingWorkflow()

    monkeypatch.setattr(orchestrator_module, 'open_run_checkpointer', tracking_open_run_checkpointer)
    monkeypatch.setattr(orchestrator_module, 'build_workflow', tracking_build_workflow)

    context = _context(tmp_path, executor)
    orchestrator = Orchestrator(
        {'workspace_dir': str(tmp_path / 'workspace')},
        brain='graph',
        graph_reasoner=_reasoner(),
    )

    try:
        first_state = orchestrator._invoke_graph_workflow(context)
        expected_thread_id = graph_thread_id(str(context.layout.challenge_dir))
        checkpoint_path = context.layout.challenge_dir / CHECKPOINT_RELATIVE_PATH
        assert first_state['paused'] is True
        assert checkpoint_path.is_file()
        assert context.metadata['graph_resume_mode'] == 'fresh'
        assert context.metadata['graph_checkpoint_found'] is False

        def forbidden_initial_workflow_state(*args, **kwargs):
            raise AssertionError('initial_workflow_state must not be called for graph checkpoint resume')

        monkeypatch.setattr(orchestrator_module, 'initial_workflow_state', forbidden_initial_workflow_state)
        context.metadata['graph_resume_requested'] = True

        resumed_state = orchestrator._invoke_graph_workflow(context)
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    assert resumed_state['paused'] is True
    assert resumed_state['phase'] in {'human-review', 'verified', 'finished', 'paused'}
    assert len(captured['opened']) == 2
    assert captured['opened'] == [context.layout.challenge_dir, context.layout.challenge_dir]
    assert captured['closed'] == captured['opened']
    assert len(captured['checkpointers']) == 2
    assert all(checkpointer is not None for checkpointer in captured['checkpointers'])
    assert captured['invocations'][0]['graph_state'] is not None
    assert captured['invocations'][0]['config'] == {'configurable': {'thread_id': expected_thread_id}}
    assert captured['invocations'][0]['kwargs']['interrupt_before'] == ['verify_candidates']
    assert captured['invocations'][1]['graph_state'] is None
    assert captured['invocations'][1]['config'] == {'configurable': {'thread_id': expected_thread_id}}
    assert captured['invocations'][1]['kwargs'] == {}
    assert context.metadata['graph_resume_requested'] is True
    assert context.metadata['graph_checkpoint_found'] is True
    assert context.metadata['graph_thread_id'] == expected_thread_id
    assert context.metadata['graph_resume_mode'] == 'resumed'
    assert expected_thread_id != context.state.challenge.id
    assert context.state.challenge.id not in expected_thread_id
    assert executor.calls == 0
    assert forbid_legacy_paths == {'llm_loop': 0, 'http': 0}
    assert str(context.layout.challenge_dir.expanduser()) not in graph_nodes._RUNTIMES


def test_resume_missing_checkpoint_fails_without_fresh_start_and_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forbid_legacy_paths):
    captured = {'closed': False, 'initial_called': False}

    import ctf_agent.core.orchestrator as orchestrator_module

    real_open_run_checkpointer = orchestrator_module.open_run_checkpointer

    @contextmanager
    def tracking_open_run_checkpointer(run_dir: Path):
        try:
            with real_open_run_checkpointer(run_dir) as checkpointer:
                yield checkpointer
        finally:
            captured['closed'] = True

    def forbidden_initial_workflow_state(*args, **kwargs):
        captured['initial_called'] = True
        raise AssertionError('missing checkpoint resume must not fresh start')

    monkeypatch.setattr(orchestrator_module, 'open_run_checkpointer', tracking_open_run_checkpointer)
    monkeypatch.setattr(orchestrator_module, 'initial_workflow_state', forbidden_initial_workflow_state)

    context = _context(tmp_path)
    context.metadata['graph_resume_requested'] = True
    orchestrator = Orchestrator(
        {'workspace_dir': str(tmp_path / 'workspace')},
        brain='graph',
        graph_reasoner=_reasoner(),
    )

    with pytest.raises(RuntimeError, match='Cannot resume graph run: no checkpoint found for this run.'):
        orchestrator._invoke_graph_workflow(context)

    assert context.metadata['graph_resume_requested'] is True
    assert context.metadata['graph_checkpoint_found'] is False
    assert context.metadata['graph_resume_mode'] == 'resumed'
    assert captured['initial_called'] is False
    assert captured['closed'] is True
    assert forbid_legacy_paths == {'llm_loop': 0, 'http': 0}
    assert str(context.layout.challenge_dir.expanduser()) not in graph_nodes._RUNTIMES


def test_resume_closes_checkpointer_when_continuation_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forbid_legacy_paths):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    captured = {'closed': False, 'invoke_config': None}

    import ctf_agent.core.orchestrator as orchestrator_module

    context = _context(tmp_path)
    orchestrator = Orchestrator(
        {'workspace_dir': str(tmp_path / 'workspace')},
        brain='graph',
        graph_reasoner=_reasoner(),
    )
    try:
        orchestrator._invoke_graph_workflow(context)
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    real_open_run_checkpointer = orchestrator_module.open_run_checkpointer

    @contextmanager
    def tracking_open_run_checkpointer(run_dir: Path):
        try:
            with real_open_run_checkpointer(run_dir) as checkpointer:
                yield checkpointer
        finally:
            captured['closed'] = True

    class FailingWorkflow:
        def invoke(self, graph_state, config=None, **kwargs):
            captured['invoke_config'] = config
            raise RuntimeError('Authorization: Bearer secret-token')

    def failing_build_workflow(*, checkpointer=None, **kwargs):
        return FailingWorkflow()

    def forbidden_initial_workflow_state(*args, **kwargs):
        raise AssertionError('initial_workflow_state must not be called for graph checkpoint resume')

    monkeypatch.setattr(orchestrator_module, 'open_run_checkpointer', tracking_open_run_checkpointer)
    monkeypatch.setattr(orchestrator_module, 'build_workflow', failing_build_workflow)
    monkeypatch.setattr(orchestrator_module, 'initial_workflow_state', forbidden_initial_workflow_state)

    context.metadata['graph_resume_requested'] = True
    with pytest.raises(RuntimeError, match='graph workflow invocation failed without fallback') as exc:
        orchestrator._invoke_graph_workflow(context)

    assert 'secret-token' not in str(exc.value)
    assert context.metadata['graph_checkpoint_found'] is True
    assert context.metadata['graph_resume_mode'] == 'resumed'
    assert captured['invoke_config'] == {'configurable': {'thread_id': graph_thread_id(str(context.layout.challenge_dir))}}
    assert captured['closed'] is True
    assert forbid_legacy_paths == {'llm_loop': 0, 'http': 0}
    assert str(context.layout.challenge_dir.expanduser()) not in graph_nodes._RUNTIMES
