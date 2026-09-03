from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ctf_agent.core.models import Artifact, Challenge


@dataclass
class SubmissionResult:
    challenge_id: str
    flag: str
    submitted: bool
    accepted: bool | None = None
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class PlatformAdapter(ABC):
    @abstractmethod
    def list_challenges(self) -> list[Challenge]:
        raise NotImplementedError

    @abstractmethod
    def get_challenge(self, challenge_id: str) -> Challenge:
        raise NotImplementedError

    @abstractmethod
    def download_files(self, challenge: Challenge, destination: str | Path) -> list[Artifact]:
        raise NotImplementedError

    @abstractmethod
    def submit_flag(self, challenge: Challenge, flag: str, *, submit: bool = False) -> SubmissionResult:
        raise NotImplementedError
