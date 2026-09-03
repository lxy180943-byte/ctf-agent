import json
from pathlib import Path

import pytest

from ctf_agent.core.models import Challenge
from ctf_agent.core.orchestrator import Orchestrator
from ctf_agent.core.state import ChallengeState
from ctf_agent.graph.checkpoint import CHECKPOINT_RELATIVE_PATH, graph_thread_id, open_run_checkpointer
from ctf_agent.pydantic_agent.agent import PydanticAISolverReasoner, SolverDependencies
from ctf_agent.pydantic_agent.models import SolverDecision
from ctf_agent.sandbox import ExecutionResult, Executor


class RecordingExecutor(Executor):
    def __init__(self) -> None:
        self.calls = 0

    def run(self, command: str, cwd: str | Path, timeout: int | None = None, env: dict[str, str] | None = None) -> ExecutionResult:
        self.calls += 1
        raise AssertionError('executor must not be called by graph resume e2e tests')


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


@pytest.fixture
def forbid_legacy_paths(monkeypatch: pytest.MonkeyPatch):
    calls = {'llm_loop': 0, 'initial_plan': 0, 'execute_plan': 0, 'http': 0}

    class ForbiddenLLMActionLoop:
        def __init__(self, *args, **kwargs) -> None:
            calls['llm_loop'] += 1
            raise AssertionError('LLMActionLoop must not be constructed by graph resume e2e')

    def forbidden_initial_plan(*args, **kwargs):
        calls['initial_plan'] += 1
        raise AssertionError('_initial_plan must not be used by graph resume e2e')

    def forbidden_execute_plan(*args, **kwargs):
        calls['execute_plan'] += 1
        raise AssertionError('_execute_plan must not be used by graph resume e2e')

    def forbidden_http(*args, **kwargs):
        calls['http'] += 1
        raise AssertionError('http_request must not be called by pause decision')

    import ctf_agent.core.orchestrator as orchestrator_module
    import ctf_agent.graph.nodes as graph_nodes

    monkeypatch.setattr(orchestrator_module, 'LLMActionLoop', ForbiddenLLMActionLoop)
    monkeypatch.setattr(Orchestrator, '_initial_plan', forbidden_initial_plan)
    monkeypatch.setattr(Orchestrator, '_execute_plan', forbidden_execute_plan)
    monkeypatch.setattr(graph_nodes, 'http_request', forbidden_http)
    return calls


def _patch_executor(monkeypatch: pytest.MonkeyPatch) -> RecordingExecutor:
    executor = RecordingExecutor()
    monkeypatch.setattr(Orchestrator, '_build_executor', lambda self, challenge, trace_store: executor)
    return executor


def _trace_actions(run_dir: Path) -> list[str]:
    return [json.loads(line)['action'] for line in (run_dir / 'trace.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]


def test_solve_pause_then_resume_from_run_dir_continues_existing_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forbid_legacy_paths):
    executor = _patch_executor(monkeypatch)
    captured = {'resume_initial_called': False, 'invoke_configs': []}

    first_orchestrator = Orchestrator(
        {'workspace_dir': str(tmp_path / 'workspace')},
        brain='graph',
        graph_reasoner=_reasoner(),
    )
    challenge = Challenge(id='graph-resume-e2e', title='Graph Resume E2E', category='misc')

    first = first_orchestrator.solve(challenge)
    run_dir = first.run_dir
    checkpoint_path = run_dir / CHECKPOINT_RELATIVE_PATH
    expected_thread_id = graph_thread_id(str(run_dir))

    assert first.state is ChallengeState.PAUSED
    assert run_dir.is_dir()
    assert (run_dir / 'state.json').is_file()
    assert checkpoint_path.is_file()
    assert first.metadata['pause_reason'] == 'Need human scope confirmation.'
    assert first.metadata['next_goal'] == 'Pause for human scope confirmation.'
    assert first.metadata['graph_thread_id'] == expected_thread_id
    assert first.metadata['graph_checkpoint_found'] is False
    assert first.metadata['graph_resume_mode'] == 'fresh'

    with open_run_checkpointer(run_dir) as checkpointer:
        assert checkpointer.get_tuple({'configurable': {'thread_id': expected_thread_id}}) is not None

    import ctf_agent.core.orchestrator as orchestrator_module

    def forbidden_initial_workflow_state(*args, **kwargs):
        captured['resume_initial_called'] = True
        raise AssertionError('resume_from_run_dir must continue checkpoint instead of fresh start')

    real_build_workflow = orchestrator_module.build_workflow

    def tracking_build_workflow(*, checkpointer=None, **kwargs):
        workflow = real_build_workflow(checkpointer=checkpointer, **kwargs)

        class TrackingWorkflow:
            def invoke(self, graph_state, config=None, **invoke_kwargs):
                captured['invoke_configs'].append({'graph_state': graph_state, 'config': config, 'kwargs': invoke_kwargs})
                return workflow.invoke(graph_state, config=config, **invoke_kwargs)

        return TrackingWorkflow()

    monkeypatch.setattr(orchestrator_module, 'initial_workflow_state', forbidden_initial_workflow_state)
    monkeypatch.setattr(orchestrator_module, 'build_workflow', tracking_build_workflow)

    before_dirs = {path.name for path in (tmp_path / 'workspace' / 'runs').iterdir() if path.is_dir()}
    resumed = Orchestrator(
        {'workspace_dir': str(tmp_path / 'workspace')},
        brain='graph',
        graph_reasoner=_reasoner(),
    ).resume_from_run_dir(run_dir)
    after_dirs = {path.name for path in (tmp_path / 'workspace' / 'runs').iterdir() if path.is_dir()}

    assert resumed.run_dir == run_dir
    assert before_dirs == after_dirs
    assert resumed.state is ChallengeState.PAUSED
    assert resumed.metadata['resumed'] is True
    assert resumed.metadata['brain'] == 'graph'
    assert resumed.metadata['brain_mode'] == 'graph'
    assert resumed.metadata['graph_thread_id'] == expected_thread_id
    assert resumed.metadata['graph']['graph_thread_id'] == expected_thread_id
    assert resumed.metadata['graph_checkpoint_found'] is True
    assert resumed.metadata['graph']['graph_checkpoint_found'] is True
    assert resumed.metadata['graph_resume_requested'] is True
    assert resumed.metadata['graph_resume_mode'] == 'resumed'
    assert resumed.metadata['pause_reason'] == 'Need human scope confirmation.'
    assert resumed.metadata['next_goal'] == 'Pause for human scope confirmation.'
    assert captured['resume_initial_called'] is False
    assert captured['invoke_configs'] == [{'graph_state': None, 'config': {'configurable': {'thread_id': expected_thread_id}}, 'kwargs': {}}]
    assert 'graph-start' in _trace_actions(run_dir)
    assert 'graph-finish' in _trace_actions(run_dir)
    assert executor.calls == 0
    assert forbid_legacy_paths == {'llm_loop': 0, 'initial_plan': 0, 'execute_plan': 0, 'http': 0}


def test_resume_from_run_dir_missing_graph_checkpoint_fails_without_fresh_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forbid_legacy_paths):
    _patch_executor(monkeypatch)
    first_orchestrator = Orchestrator(
        {'workspace_dir': str(tmp_path / 'workspace')},
        brain='graph',
        graph_reasoner=_reasoner(),
    )
    first = first_orchestrator.solve(Challenge(id='graph-resume-missing-checkpoint', title='Graph Missing Checkpoint', category='misc'))
    run_dir = first.run_dir
    checkpoint_path = run_dir / CHECKPOINT_RELATIVE_PATH
    checkpoint_path.unlink()

    import ctf_agent.core.orchestrator as orchestrator_module

    def forbidden_initial_workflow_state(*args, **kwargs):
        raise AssertionError('missing checkpoint public resume must not fresh start')

    monkeypatch.setattr(orchestrator_module, 'initial_workflow_state', forbidden_initial_workflow_state)
    before_dirs = {path.name for path in (tmp_path / 'workspace' / 'runs').iterdir() if path.is_dir()}

    resumed = Orchestrator(
        {'workspace_dir': str(tmp_path / 'workspace')},
        brain='graph',
        graph_reasoner=_reasoner(),
    ).resume_from_run_dir(run_dir)

    after_dirs = {path.name for path in (tmp_path / 'workspace' / 'runs').iterdir() if path.is_dir()}
    expected_thread_id = graph_thread_id(str(run_dir))

    assert resumed.run_dir == run_dir
    assert resumed.state is ChallengeState.FAILED
    assert before_dirs == after_dirs
    assert resumed.metadata['resumed'] is True
    assert resumed.metadata['brain'] == 'graph'
    assert resumed.metadata['graph_failure_reason'] == 'Cannot resume graph run: no checkpoint found for this run.'
    assert resumed.metadata['graph']['failure_reason'] == 'Cannot resume graph run: no checkpoint found for this run.'
    assert checkpoint_path.is_file()
    with open_run_checkpointer(run_dir) as checkpointer:
        assert checkpointer.get_tuple({'configurable': {'thread_id': expected_thread_id}}) is None
    assert 'graph-error' in _trace_actions(run_dir)
    assert forbid_legacy_paths == {'llm_loop': 0, 'initial_plan': 0, 'execute_plan': 0, 'http': 0}
