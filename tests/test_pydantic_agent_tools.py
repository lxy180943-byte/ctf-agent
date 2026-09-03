"""Offline policy tests for PydanticAI structured tool adapters."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ctf_agent.agents import AgentContext
from ctf_agent.core.models import Challenge
from ctf_agent.core.state import ChallengeState
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.pydantic_agent.tools import (
    HttpRequestInput,
    PauseForHumanInput,
    ReadFileInput,
    RunCommandInput,
    ToolDependencies,
    http_request,
    pause_for_human,
    read_file,
    run_command,
)
from ctf_agent.sandbox import LocalExecutor
from ctf_agent.tools import default_registry


def _deps(tmp_path: Path, *, config: dict | None = None) -> ToolDependencies:
    challenge = Challenge(
        id="pydantic-tools",
        title="Pydantic Tools",
        category="web",
        connection="http://127.0.0.1:18080",
        files=["note.txt"],
    )
    manager = WorkspaceManager(tmp_path / "workspace")
    state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    (layout.work_dir / "note.txt").write_text("<title>Evidence</title> local marker", encoding="utf-8")
    context = AgentContext(
        state=state,
        layout=layout,
        trace_store=manager.trace_store_for(challenge.id),
        executor=LocalExecutor(manager.workspace_root, trace_store=manager.trace_store_for(challenge.id), challenge_id=challenge.id),
        tool_registry=default_registry(),
        config=config or {},
        max_steps=5,
        timeout=10,
    )
    return ToolDependencies(context=context)


def test_read_file_returns_structured_evidence_trace_and_artifact(tmp_path: Path) -> None:
    deps = _deps(tmp_path)

    result = read_file(deps, ReadFileInput(path="work/note.txt"))

    assert result.ok is True
    assert result.observation["title"] == "Evidence"
    assert result.artifacts
    assert Path(result.artifacts[-1]["path"]).exists()
    events = deps.context.trace_store.read_events()
    assert events[-1].action == "read_file"
    assert events[-1].metadata["risk"] == "low"


def test_read_file_refuses_outside_challenge_workspace(tmp_path: Path) -> None:
    deps = _deps(tmp_path)

    result = read_file(deps, ReadFileInput(path="../../outside.txt"))

    assert result.ok is False
    assert result.risk == "refuse"
    assert "outside workspace" in (result.error or "")


def test_http_request_is_denied_without_explicit_network_authorization(tmp_path: Path) -> None:
    deps = _deps(tmp_path, config={"sandbox": {"allow_network": False}})

    result = http_request(deps, HttpRequestInput(url="http://127.0.0.1:18080/"))

    assert result.ok is False
    assert result.risk == "high"
    assert "authorization" in (result.error or "")
    assert deps.execution_batch.results == []


def test_http_request_rejects_invalid_header_without_execution(tmp_path: Path) -> None:
    deps = _deps(tmp_path, config={"sandbox": {"allow_network": True}})

    result = http_request(
        deps,
        HttpRequestInput(url="http://127.0.0.1:18080/", headers={"X-Test": "ok\nInjected: value"}),
    )

    assert result.ok is False
    assert "headers" in (result.error or "")
    assert deps.execution_batch.results == []


def test_run_command_is_fallback_and_high_risk_command_is_not_executed(tmp_path: Path) -> None:
    deps = _deps(tmp_path)

    result = run_command(deps, RunCommandInput(command="rm -rf ./note.txt"))

    assert result.ok is False
    assert result.risk == "high"
    assert (deps.context.layout.work_dir / "note.txt").exists()


def test_pause_records_reason_without_marking_solved(tmp_path: Path) -> None:
    deps = _deps(tmp_path)

    result = pause_for_human(deps, PauseForHumanInput(reason="Need authorized target confirmation."))

    assert result.ok is True
    assert deps.context.state.state is ChallengeState.PAUSED
    assert deps.context.state.flag_candidates == []


def test_tool_inputs_reject_unknown_parameters() -> None:
    with pytest.raises(ValidationError):
        RunCommandInput(command="true", unexpected="value")
