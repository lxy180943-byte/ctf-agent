"""Run-directory LangGraph checkpoint helpers."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Iterator

from ctf_agent.core.redaction import redact_value

CHECKPOINT_RELATIVE_PATH = Path("graph") / "checkpoints.sqlite"


def graph_thread_id(run_id: str) -> str:
    digest = sha256(str(run_id).encode("utf-8")).hexdigest()
    return f"ctf-agent-graph-{digest}"


def _sqlite_saver():
    from langgraph.checkpoint.sqlite import SqliteSaver

    return SqliteSaver


@contextmanager
def open_run_checkpointer(run_dir: Path) -> Iterator[object]:
    checkpoint_path = Path(run_dir).expanduser() / CHECKPOINT_RELATIVE_PATH
    manager = None
    try:
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        manager = _sqlite_saver().from_conn_string(str(checkpoint_path))
        checkpointer = manager.__enter__()
        checkpointer.setup()
    except Exception as exc:
        if manager is not None:
            manager.__exit__(type(exc), exc, exc.__traceback__)
        message = redact_value(str(exc))
        raise RuntimeError(f"failed to open run graph checkpointer: {message}") from exc
    try:
        yield checkpointer
    finally:
        manager.__exit__(None, None, None)
