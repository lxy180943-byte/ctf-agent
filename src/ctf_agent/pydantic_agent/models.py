"""Strict Pydantic contracts for GPT/Claude CTF reasoning decisions."""

from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Hypothesis(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    claim: str = Field(min_length=1, max_length=2000)
    evidence_for: list[str] = Field(default_factory=list, max_length=30)
    evidence_against: list[str] = Field(default_factory=list, max_length=30)
    confidence: float = Field(ge=0.0, le=1.0)
    falsification_test: str = Field(min_length=1, max_length=1200)


class ActionInput(StrictModel):
    type: str


class _RelativePathInput(ActionInput):
    path: str = Field(min_length=1, max_length=1024)

    @field_validator("path")
    @classmethod
    def require_relative_path(cls, value: str) -> str:
        if Path(value).is_absolute() or PureWindowsPath(value).is_absolute():
            raise ValueError("path must be relative to the challenge workspace")
        return value


class ReadFileInput(_RelativePathInput):
    type: Literal["read_file"] = "read_file"


class SearchArtifactsInput(ActionInput):
    type: Literal["search_artifacts"] = "search_artifacts"
    pattern: str = Field(min_length=1, max_length=512)


class RunCommandInput(ActionInput):
    type: Literal["run_command"] = "run_command"
    command: str = Field(min_length=1, max_length=8000)
    timeout: int = Field(default=60, ge=1, le=600)


class HttpRequestInput(ActionInput):
    type: Literal["http_request"] = "http_request"
    method: Literal["GET", "POST", "HEAD"] = "GET"
    url: str = Field(min_length=1, max_length=4096)
    params: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = Field(default=None, max_length=100_000)
    timeout: int = Field(default=20, ge=1, le=600)


class InspectBinaryInput(_RelativePathInput):
    type: Literal["inspect_binary"] = "inspect_binary"


class AskVerifierInput(ActionInput):
    type: Literal["ask_verifier"] = "ask_verifier"


class PauseForHumanInput(ActionInput):
    type: Literal["pause"] = "pause"
    reason: str = Field(min_length=1, max_length=2000)


ExperimentActionInput = Annotated[
    Union[ReadFileInput, SearchArtifactsInput, RunCommandInput, HttpRequestInput, InspectBinaryInput, AskVerifierInput, PauseForHumanInput],
    Field(discriminator="type"),
]
ExperimentActionType = Literal["read_file", "search_artifacts", "run_command", "http_request", "inspect_binary", "ask_verifier", "pause"]


class ExperimentPlan(StrictModel):
    goal: str = Field(min_length=1, max_length=1200)
    action_type: ExperimentActionType
    action_input: ExperimentActionInput
    expected_signal: str = Field(min_length=1, max_length=1200)
    failure_signal: str = Field(min_length=1, max_length=1200)
    risk: Literal["low", "medium", "high"]
    rollback: str = Field(min_length=1, max_length=1200)

    @model_validator(mode="after")
    def require_matching_action_input(self) -> "ExperimentPlan":
        if self.action_type != self.action_input.type:
            raise ValueError("action_type must match action_input.type")
        return self


class SolverDecision(StrictModel):
    """A proposed next step, never an execution result or verified flag."""

    current_hypothesis: Hypothesis
    confirmed_facts: list[str] = Field(default_factory=list, max_length=50)
    unknowns: list[str] = Field(default_factory=list, max_length=50)
    candidate_chains: list[list[str]] = Field(default_factory=list, max_length=20)
    selected_experiment: ExperimentPlan | None = None
    next_action: Literal["execute_selected_experiment", "ask_human", "pause", "stop"]
    need_human: bool = False
    stop_reason: str | None = Field(default=None, max_length=1200)

    @model_validator(mode="after")
    def enforce_reasoning_only_contract(self) -> "SolverDecision":
        if self.next_action == "execute_selected_experiment" and self.selected_experiment is None:
            raise ValueError("execute_selected_experiment requires selected_experiment")
        if self.next_action != "execute_selected_experiment" and self.selected_experiment is not None:
            raise ValueError("selected_experiment is only allowed when it is the next action")
        if self.next_action == "ask_human" and not self.need_human:
            raise ValueError("ask_human requires need_human=true")
        if self.need_human and self.next_action != "ask_human":
            raise ValueError("need_human=true requires next_action=ask_human")
        text = "\n".join(str(value) for value in self.model_dump().values())
        if _FLAG_PATTERN.search(text):
            raise ValueError("solver decisions may not return a flag value")
        if _EXECUTION_CLAIM_PATTERN.search(text):
            raise ValueError("solver decisions may not claim tools were executed")
        return self


_FLAG_PATTERN = re.compile(r"(?:flag|ctf)\{[^}\n]{1,512}\}", re.IGNORECASE)
_EXECUTION_CLAIM_PATTERN = re.compile(r"(?:i|we)\s+(?:ran|executed|used)|tool\s+(?:was\s+)?executed|command\s+output|already\s+(?:ran|executed)|已执行|执行过|工具输出", re.IGNORECASE)
