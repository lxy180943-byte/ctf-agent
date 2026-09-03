from __future__ import annotations

from dataclasses import dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: _serialize(item) for key, item in value.__dict__.items()}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    return value


@dataclass
class Challenge:
    id: str
    title: str
    category: str
    description: str = ""
    files: list[str] = field(default_factory=list)
    connection: str | None = None
    hints: list[str] = field(default_factory=list)
    flag_regex: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Challenge:
        return cls(
            id=str(data["id"]),
            title=str(data["title"]),
            category=str(data["category"]),
            description=str(data.get("description", "")),
            files=[str(item) for item in data.get("files", [])],
            connection=data.get("connection"),
            hints=[str(item) for item in data.get("hints", [])],
            flag_regex=data.get("flag_regex"),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class Artifact:
    path: str
    kind: str = "file"
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Artifact:
        return cls(
            path=str(data["path"]),
            kind=str(data.get("kind", "file")),
            description=str(data.get("description", "")),
            metadata=dict(data.get("metadata", {})),
            created_at=str(data.get("created_at", utc_now())),
        )


@dataclass
class Observation:
    summary: str
    raw: str | None = None
    source: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Observation:
        return cls(
            summary=str(data["summary"]),
            raw=data.get("raw"),
            source=data.get("source"),
            metadata=dict(data.get("metadata", {})),
            created_at=str(data.get("created_at", utc_now())),
        )


@dataclass
class FlagCandidate:
    value: str
    source: str
    confidence: float = 0.0
    verified: bool = False
    submitted: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FlagCandidate:
        return cls(
            value=str(data["value"]),
            source=str(data.get("source", "unknown")),
            confidence=float(data.get("confidence", 0.0)),
            verified=bool(data.get("verified", False)),
            submitted=bool(data.get("submitted", False)),
            metadata=dict(data.get("metadata", {})),
            created_at=str(data.get("created_at", utc_now())),
        )


@dataclass
class Step:
    agent: str
    action: str
    command: list[str] | None = None
    observations: list[Observation] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    exit_code: int | None = None
    started_at: str = field(default_factory=utc_now)
    ended_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    def finish(self, exit_code: int | None = None) -> None:
        self.exit_code = exit_code
        self.ended_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Step:
        return cls(
            agent=str(data["agent"]),
            action=str(data["action"]),
            command=[str(item) for item in data["command"]] if data.get("command") is not None else None,
            observations=[Observation.from_dict(item) for item in data.get("observations", [])],
            artifacts=[Artifact.from_dict(item) for item in data.get("artifacts", [])],
            exit_code=data.get("exit_code"),
            started_at=str(data.get("started_at", utc_now())),
            ended_at=data.get("ended_at"),
            metadata=dict(data.get("metadata", {})),
            id=str(data.get("id", uuid4().hex)),
        )


@dataclass
class Attempt:
    challenge_id: str
    number: int = 1
    steps: list[Step] = field(default_factory=list)
    flag_candidates: list[FlagCandidate] = field(default_factory=list)
    started_at: str = field(default_factory=utc_now)
    ended_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    def add_step(self, step: Step) -> None:
        self.steps.append(step)

    def add_flag_candidate(self, candidate: FlagCandidate) -> None:
        self.flag_candidates.append(candidate)

    def finish(self) -> None:
        self.ended_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Attempt:
        return cls(
            challenge_id=str(data["challenge_id"]),
            number=int(data.get("number", 1)),
            steps=[Step.from_dict(item) for item in data.get("steps", [])],
            flag_candidates=[FlagCandidate.from_dict(item) for item in data.get("flag_candidates", [])],
            started_at=str(data.get("started_at", utc_now())),
            ended_at=data.get("ended_at"),
            metadata=dict(data.get("metadata", {})),
            id=str(data.get("id", uuid4().hex)),
        )
