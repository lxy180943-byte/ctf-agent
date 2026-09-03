from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ctf_agent.core.models import Attempt, Challenge, FlagCandidate, utc_now


class ChallengeState(str, Enum):
    NEW = "new"
    ANALYZING = "analyzing"
    RUNNING = "running"
    VERIFYING = "verifying"
    SOLVED = "solved"
    FAILED = "failed"
    PAUSED = "paused"


ALLOWED_TRANSITIONS: dict[ChallengeState, set[ChallengeState]] = {
    ChallengeState.NEW: {ChallengeState.ANALYZING, ChallengeState.PAUSED, ChallengeState.FAILED},
    ChallengeState.ANALYZING: {ChallengeState.RUNNING, ChallengeState.VERIFYING, ChallengeState.PAUSED, ChallengeState.FAILED},
    ChallengeState.RUNNING: {ChallengeState.ANALYZING, ChallengeState.VERIFYING, ChallengeState.PAUSED, ChallengeState.FAILED},
    ChallengeState.VERIFYING: {
        ChallengeState.ANALYZING,
        ChallengeState.RUNNING,
        ChallengeState.SOLVED,
        ChallengeState.PAUSED,
        ChallengeState.FAILED,
    },
    ChallengeState.PAUSED: {ChallengeState.ANALYZING, ChallengeState.RUNNING, ChallengeState.VERIFYING, ChallengeState.FAILED},
    ChallengeState.SOLVED: set(),
    ChallengeState.FAILED: {ChallengeState.ANALYZING},
}


class InvalidStateTransition(ValueError):
    """Raised when a challenge run attempts an invalid state transition."""


@dataclass
class ChallengeRunState:
    challenge: Challenge
    state: ChallengeState = ChallengeState.NEW
    attempts: list[Attempt] = field(default_factory=list)
    flag_candidates: list[FlagCandidate] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def transition_to(self, next_state: ChallengeState | str) -> None:
        next_state = ChallengeState(next_state)
        if next_state == self.state:
            return
        if next_state not in ALLOWED_TRANSITIONS[self.state]:
            raise InvalidStateTransition(f"Cannot transition challenge {self.challenge.id} from {self.state.value} to {next_state.value}")
        self.state = next_state
        self.updated_at = utc_now()

    def start_attempt(self) -> Attempt:
        attempt = Attempt(challenge_id=self.challenge.id, number=len(self.attempts) + 1)
        self.attempts.append(attempt)
        self.updated_at = utc_now()
        return attempt

    def add_flag_candidate(self, candidate: FlagCandidate) -> None:
        self.flag_candidates.append(candidate)
        self.updated_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "challenge": self.challenge.to_dict(),
            "state": self.state.value,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "flag_candidates": [candidate.to_dict() for candidate in self.flag_candidates],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChallengeRunState:
        return cls(
            challenge=Challenge.from_dict(data["challenge"]),
            state=ChallengeState(data.get("state", ChallengeState.NEW.value)),
            attempts=[Attempt.from_dict(item) for item in data.get("attempts", [])],
            flag_candidates=[FlagCandidate.from_dict(item) for item in data.get("flag_candidates", [])],
            created_at=str(data.get("created_at", utc_now())),
            updated_at=str(data.get("updated_at", utc_now())),
            metadata=dict(data.get("metadata", {})),
        )
