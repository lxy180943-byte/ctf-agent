from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from ctf_agent.core.models import Artifact, Step, utc_now
from ctf_agent.core.redaction import redact_value

SUMMARY_LIMIT = 4000


def summarize_text(value: str | None, limit: int = SUMMARY_LIMIT) -> str | None:
    if value is None:
        return None
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    return f"{value[:limit]}\n... <truncated {omitted} chars>"


@dataclass
class TraceEvent:
    challenge_id: str
    agent: str
    action: str
    command: list[str] | None = None
    stdout: str | None = None
    stderr: str | None = None
    artifacts: list[Artifact] = field(default_factory=list)
    exit_code: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    timestamp: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "timestamp": self.timestamp,
            "challenge_id": self.challenge_id,
            "agent": self.agent,
            "action": self.action,
            "command": self.command,
            "stdout": summarize_text(self.stdout),
            "stderr": summarize_text(self.stderr),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "metadata": self.metadata,
        }
        return redact_value(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TraceEvent:
        return cls(
            challenge_id=str(data["challenge_id"]),
            agent=str(data["agent"]),
            action=str(data["action"]),
            command=[str(item) for item in data["command"]] if data.get("command") is not None else None,
            stdout=data.get("stdout"),
            stderr=data.get("stderr"),
            artifacts=[Artifact.from_dict(item) for item in data.get("artifacts", [])],
            exit_code=data.get("exit_code"),
            started_at=data.get("started_at"),
            ended_at=data.get("ended_at"),
            timestamp=str(data.get("timestamp", utc_now())),
            metadata=dict(data.get("metadata", {})),
            id=str(data.get("id", uuid4().hex)),
        )


class TraceStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    def append(self, event: TraceEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")

    def append_step(self, challenge_id: str, step: Step, stdout: str | None = None, stderr: str | None = None) -> TraceEvent:
        event = TraceEvent(
            challenge_id=challenge_id,
            agent=step.agent,
            action=step.action,
            command=step.command,
            stdout=stdout,
            stderr=stderr,
            artifacts=step.artifacts,
            exit_code=step.exit_code,
            started_at=step.started_at,
            ended_at=step.ended_at,
            metadata={"step_id": step.id, **step.metadata},
        )
        self.append(event)
        return event

    def read_events(self) -> list[TraceEvent]:
        if not self.path.exists():
            return []
        events: list[TraceEvent] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    events.append(TraceEvent.from_dict(json.loads(stripped)))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSONL trace at {self.path}:{line_number}") from exc
        return events
