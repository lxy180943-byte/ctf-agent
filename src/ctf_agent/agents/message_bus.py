from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ctf_agent.core.models import FlagCandidate, utc_now


@dataclass
class AgentMessage:
    kind: str
    author: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "author": self.author,
            "content": self.content,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


class AgentMessageBus:
    def __init__(self) -> None:
        self.messages: list[AgentMessage] = []
        self.flag_candidates: list[FlagCandidate] = []

    def publish(self, kind: str, author: str, content: str, **metadata: Any) -> AgentMessage:
        message = AgentMessage(kind=kind, author=author, content=content, metadata=metadata)
        self.messages.append(message)
        return message

    def add_hypothesis(self, author: str, content: str, **metadata: Any) -> AgentMessage:
        return self.publish("hypothesis", author, content, **metadata)

    def add_observation(self, author: str, content: str, **metadata: Any) -> AgentMessage:
        return self.publish("observation", author, content, **metadata)

    def add_failure(self, author: str, content: str, **metadata: Any) -> AgentMessage:
        return self.publish("failure_reason", author, content, **metadata)

    def add_flag_candidate(self, author: str, candidate: FlagCandidate) -> None:
        self.flag_candidates.append(candidate)
        self.publish("flag_candidate", author, candidate.value, candidate=candidate.to_dict())

    def by_kind(self, kind: str) -> list[AgentMessage]:
        return [message for message in self.messages if message.kind == kind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": [message.to_dict() for message in self.messages],
            "flag_candidates": [candidate.to_dict() for candidate in self.flag_candidates],
        }
