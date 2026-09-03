from pathlib import Path
from typing import TypedDict

import pytest

from langgraph.graph import END, START, StateGraph

from ctf_agent.graph import graph_thread_id, open_run_checkpointer
from ctf_agent.graph.checkpoint import CHECKPOINT_RELATIVE_PATH


class CounterState(TypedDict):
    count: int


def increment(state: CounterState) -> dict[str, int]:
    return {"count": state["count"] + 1}


def _counter_graph(checkpointer):
    builder = StateGraph(CounterState)
    builder.add_node("increment", increment)
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    return builder.compile(checkpointer=checkpointer)


def test_run_checkpointer_persists_state_in_run_dir_and_reopens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def blocked_socket(*args, **kwargs):
        raise AssertionError("checkpoint test must not open network sockets")

    import socket

    monkeypatch.setattr(socket, "socket", blocked_socket)
    run_dir = tmp_path / "workspace" / "runs" / "run-1"
    run_id = "run-1-unique-id"
    thread_id = graph_thread_id(run_id)
    config = {"configurable": {"thread_id": thread_id}}
    checkpoint_path = run_dir / CHECKPOINT_RELATIVE_PATH

    with open_run_checkpointer(run_dir) as checkpointer:
        graph = _counter_graph(checkpointer)
        assert graph.invoke({"count": 1}, config=config) == {"count": 2}
        checkpoint = checkpointer.get_tuple(config)
        assert checkpoint is not None
        assert checkpoint.checkpoint["channel_values"]["count"] == 2

    assert checkpoint_path == run_dir / "graph" / "checkpoints.sqlite"
    assert checkpoint_path.is_file()

    with open_run_checkpointer(run_dir) as checkpointer:
        checkpoint = checkpointer.get_tuple(config)
        assert checkpoint is not None
        assert checkpoint.checkpoint["channel_values"]["count"] == 2
        graph = _counter_graph(checkpointer)
        assert graph.invoke(None, config=config) == {"count": 2}

    with open_run_checkpointer(run_dir) as checkpointer:
        checkpoint = checkpointer.get_tuple(config)
        assert checkpoint is not None
        assert checkpoint.checkpoint["channel_values"]["count"] == 2

    assert graph_thread_id("another-run-unique-id") != thread_id
    assert graph_thread_id(run_id) == thread_id
    for file_path in run_dir.rglob("*"):
        assert file_path == run_dir or run_dir in file_path.parents


def test_open_run_checkpointer_supports_repeated_open_and_sanitizes_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run_dir = tmp_path / "run-secret"
    with open_run_checkpointer(run_dir) as first:
        assert first is not None
    with open_run_checkpointer(run_dir) as second:
        assert second is not None

    import ctf_agent.graph.checkpoint as checkpoint_module

    class BrokenSaver:
        @classmethod
        def from_conn_string(cls, conn_string: str):
            raise RuntimeError("Authorization: Bearer secret-token-value")

    monkeypatch.setattr(checkpoint_module, "_sqlite_saver", lambda: BrokenSaver)
    with pytest.raises(RuntimeError, match="failed to open run graph checkpointer") as exc:
        with checkpoint_module.open_run_checkpointer(tmp_path / "broken"):
            pass
    assert "secret-token-value" not in str(exc.value)
