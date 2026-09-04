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
from ctf_agent.graph.state import WorkflowState
from ctf_agent.pydantic_agent.agent import PydanticAISolverReasoner, SolverDependencies
from ctf_agent.pydantic_agent.models import SolverDecision
from ctf_agent.sandbox import ExecutionResult, Executor
from ctf_agent.tools import default_registry


class RecordingExecutor(Executor):
    def __init__(self) -> None:
        self.calls = 0

    def run(self, command: str, cwd: str | Path, timeout: int | None = None, env: dict[str, str] | None = None) -> ExecutionResult:
        self.calls += 1
        raise AssertionError('executor must not be called by graph checkpoint injection')


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


def _context(tmp_path: Path, executor: RecordingExecutor | None = None, challenge_id: str = 'raw-challenge-id') -> AgentContext:
    manager = WorkspaceManager(tmp_path / 'workspace')
    challenge = Challenge(id=challenge_id, title='Graph Checkpoint Injection', category='misc')
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


def test_invoke_graph_workflow_injects_run_dir_checkpointer_and_hashed_thread_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    captured: dict[str, object] = {}
    executor = RecordingExecutor()

    import ctf_agent.core.orchestrator as orchestrator_module

    real_open_run_checkpointer = orchestrator_module.open_run_checkpointer
    real_build_workflow = orchestrator_module.build_workflow

    @contextmanager
    def tracking_open_run_checkpointer(run_dir: Path):
        captured['opened_run_dir'] = Path(run_dir)
        try:
            with real_open_run_checkpointer(run_dir) as checkpointer:
                captured['entered'] = True
                try:
                    yield checkpointer
                finally:
                    captured['exiting'] = True
        finally:
            captured['closed'] = True

    def tracking_build_workflow(*, checkpointer=None, **kwargs):
        captured['build_checkpointer'] = checkpointer
        captured['build_kwargs'] = kwargs
        workflow = real_build_workflow(checkpointer=checkpointer, **kwargs)

        class TrackingWorkflow:
            def invoke(self, graph_state, config=None, **invoke_kwargs):
                captured['invoke_config'] = config
                captured['invoke_kwargs'] = invoke_kwargs
                return workflow.invoke(graph_state, config=config, **invoke_kwargs)

        return TrackingWorkflow()

    class ForbiddenLLMActionLoop:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError('LLMActionLoop must not be constructed by graph checkpoint injection')

    def http_request(*args, **kwargs):
        raise AssertionError('http_request must not be called by pause decision')

    monkeypatch.setattr(orchestrator_module, 'open_run_checkpointer', tracking_open_run_checkpointer)
    monkeypatch.setattr(orchestrator_module, 'build_workflow', tracking_build_workflow)
    monkeypatch.setattr(orchestrator_module, 'LLMActionLoop', ForbiddenLLMActionLoop)
    monkeypatch.setattr(graph_nodes, 'http_request', http_request)

    context = _context(tmp_path, executor)
    orchestrator = Orchestrator(
        {'workspace_dir': str(tmp_path / 'workspace')},
        brain='graph',
        graph_reasoner=_reasoner(),
    )

    try:
        final_state: WorkflowState = orchestrator._invoke_graph_workflow(context)
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    checkpoint_path = context.layout.challenge_dir / CHECKPOINT_RELATIVE_PATH
    expected_thread_id = graph_thread_id(str(context.layout.challenge_dir))

    assert isinstance(final_state, dict)
    assert final_state['paused'] is True
    assert checkpoint_path.is_file()
    assert captured['opened_run_dir'] == context.layout.challenge_dir
    assert captured['entered'] is True
    assert captured['build_checkpointer'] is not None
    assert captured['build_kwargs'] == {
        'command_timeout_seconds': 60,
        'max_tool_calls': 10,
        'max_network_requests': 12,
        'run_timeout_seconds': 1800,
        'max_repeated_actions': 3,
        'max_consecutive_failures': 3,
    }
    assert captured['invoke_config'] == {'configurable': {'thread_id': expected_thread_id}}
    assert captured['invoke_kwargs']['interrupt_before'] == ['verify_candidates']
    assert expected_thread_id != context.state.challenge.id
    assert context.state.challenge.id not in expected_thread_id
    assert captured['exiting'] is True
    assert captured['closed'] is True
    assert executor.calls == 0
    assert str(context.layout.challenge_dir.expanduser()) not in graph_nodes._RUNTIMES


def test_invoke_graph_workflow_closes_checkpointer_when_graph_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}

    import ctf_agent.core.orchestrator as orchestrator_module

    real_open_run_checkpointer = orchestrator_module.open_run_checkpointer

    @contextmanager
    def tracking_open_run_checkpointer(run_dir: Path):
        try:
            with real_open_run_checkpointer(run_dir) as checkpointer:
                try:
                    yield checkpointer
                finally:
                    captured['exiting'] = True
        finally:
            captured['closed'] = True

    class FailingWorkflow:
        def invoke(self, *args, **kwargs):
            raise ValueError('Authorization: Bearer secret-token')

    def failing_build_workflow(*, checkpointer=None, **kwargs):
        captured['build_checkpointer'] = checkpointer
        return FailingWorkflow()

    class ForbiddenLLMActionLoop:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError('LLMActionLoop must not be constructed by graph checkpoint injection')

    monkeypatch.setattr(orchestrator_module, 'open_run_checkpointer', tracking_open_run_checkpointer)
    monkeypatch.setattr(orchestrator_module, 'build_workflow', failing_build_workflow)
    monkeypatch.setattr(orchestrator_module, 'LLMActionLoop', ForbiddenLLMActionLoop)

    context = _context(tmp_path)
    orchestrator = Orchestrator(
        {'workspace_dir': str(tmp_path / 'workspace')},
        brain='graph',
        graph_reasoner=_reasoner(),
    )

    with pytest.raises(RuntimeError, match='graph workflow invocation failed without fallback') as exc:
        orchestrator._invoke_graph_workflow(context)

    assert 'secret-token' not in str(exc.value)
    assert captured['build_checkpointer'] is not None
    assert captured['exiting'] is True
    assert captured['closed'] is True
    assert (context.layout.challenge_dir / CHECKPOINT_RELATIVE_PATH).is_file()
    assert str(context.layout.challenge_dir.expanduser()) not in graph_nodes._RUNTIMES
