from pathlib import Path

import pytest

from ctf_agent.cli.app import build_parser
from ctf_agent.core.config import (
    DEFAULT_GRAPH_BUDGETS,
    ConfigError,
    load_config,
    resolve_graph_budgets,
)
from ctf_agent.core.orchestrator import Orchestrator, _CommandTimeoutExecutor
from ctf_agent.graph.builder import build_workflow
from ctf_agent.sandbox import ExecutionResult, Executor


class RecordingExecutor(Executor):
    def __init__(self) -> None:
        self.timeouts: list[int | None] = []

    def run(self, command, cwd, timeout=None, env=None):
        self.timeouts.append(timeout)
        return ExecutionResult(
            command=command,
            cwd=str(cwd),
            env=dict(env or {}),
            timeout=int(timeout or 0),
            exit_code=0,
            stdout="",
            stderr="",
            started_at="2026-01-01T00:00:00+00:00",
            ended_at="2026-01-01T00:00:00+00:00",
            duration_seconds=0.0,
        )


def _write_config(path: Path, budget_lines: str = "") -> Path:
    text = "workspace_dir: ~/ctf-workspace\n"
    if budget_lines:
        text += f"graph:\n  budgets:\n{budget_lines}"
    path.write_text(text, encoding="utf-8")
    return path


def test_default_graph_budget_contract_is_normalized_by_load_config(tmp_path: Path):
    config = load_config(_write_config(tmp_path / "config.yaml"), environ={})

    assert config["graph"]["budgets"] == DEFAULT_GRAPH_BUDGETS.to_dict()


def test_partial_config_override_uses_defaults_for_missing_and_none_values(tmp_path: Path):
    config = load_config(
        _write_config(
            tmp_path / "config.yaml",
            "    command_timeout_seconds: 17\n    run_timeout_seconds: null\n",
        ),
        environ={},
    )

    assert config["graph"]["budgets"] == {
        **DEFAULT_GRAPH_BUDGETS.to_dict(),
        "command_timeout_seconds": 17,
    }


@pytest.mark.parametrize("value", [True, False, 1.5, "10", [], {}])
def test_invalid_graph_budget_values_are_rejected(value):
    with pytest.raises(ConfigError, match="must be a positive integer"):
        resolve_graph_budgets({"graph": {"budgets": {"max_tool_calls": value}}})


@pytest.mark.parametrize("value", [0, -1])
def test_zero_and_negative_graph_budgets_are_rejected(value):
    with pytest.raises(ConfigError, match="must be greater than zero"):
        resolve_graph_budgets({"graph": {"budgets": {"max_network_requests": value}}})


def test_builder_rejects_invalid_direct_budget_override():
    with pytest.raises(ConfigError, match="max_tool_calls"):
        build_workflow(max_tool_calls=0)


def test_environment_then_explicit_orchestrator_override_precedence(tmp_path: Path):
    config = load_config(
        _write_config(tmp_path / "config.yaml", "    max_tool_calls: 4\n    command_timeout_seconds: 11\n"),
        environ={
            "CTF_AGENT_CONFIG__GRAPH__BUDGETS__MAX_TOOL_CALLS": "5",
            "CTF_AGENT_CONFIG__GRAPH__BUDGETS__COMMAND_TIMEOUT_SECONDS": "12",
        },
    )
    orchestrator = Orchestrator(config, max_steps=6, timeout=13, brain="graph")

    assert config["graph"]["budgets"]["max_tool_calls"] == 5
    assert config["graph"]["budgets"]["command_timeout_seconds"] == 12
    assert orchestrator.graph_budgets.max_tool_calls == 6
    assert orchestrator.graph_budgets.command_timeout_seconds == 13


def test_all_explicit_graph_budget_values_reach_orchestrator():
    expected = {
        "command_timeout_seconds": 7,
        "run_timeout_seconds": 101,
        "max_tool_calls": 11,
        "max_network_requests": 13,
        "max_repeated_actions": 3,
        "max_consecutive_failures": 5,
    }

    orchestrator = Orchestrator({"graph": {"budgets": expected}}, brain="graph")

    assert orchestrator.graph_budgets.to_dict() == expected


def test_non_graph_modes_keep_legacy_defaults():
    orchestrator = Orchestrator({"sandbox": {"timeout_seconds": 19}}, brain="fallback")

    assert orchestrator.max_steps == 10
    assert orchestrator.timeout == 19


def test_cli_omission_preserves_config_and_explicit_flags_are_available():
    parser = build_parser()

    omitted = parser.parse_args(["solve", "challenge"])
    explicit = parser.parse_args(["solve", "challenge", "--max-steps", "7", "--timeout", "8"])

    assert omitted.max_steps is None
    assert omitted.timeout is None
    assert explicit.max_steps == 7
    assert explicit.timeout == 8


def test_command_timeout_wrapper_caps_requested_timeout(tmp_path: Path):
    delegate = RecordingExecutor()
    executor = _CommandTimeoutExecutor(delegate, command_timeout_seconds=9)

    executor.run("one", tmp_path, timeout=30)
    executor.run("two", tmp_path, timeout=4)
    executor.run("three", tmp_path)

    assert delegate.timeouts == [9, 4, 9]
