from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ctf_agent.core.state import ChallengeRunState
from ctf_agent.core.trace import TraceStore
from ctf_agent.core.workspace import WorkspaceLayout
from ctf_agent.llm import LLMProvider, PromptStore
from ctf_agent.sandbox import Executor
from ctf_agent.tools import ToolRegistry


@dataclass
class AgentContext:
    state: ChallengeRunState
    layout: WorkspaceLayout
    trace_store: TraceStore
    executor: Executor
    tool_registry: ToolRegistry
    config: dict[str, Any]
    max_steps: int
    timeout: int
    llm_provider: LLMProvider | None = None
    prompt_store: PromptStore | None = None
    message_bus: Any | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class Agent(ABC):
    name: str
    role: str

    def __init__(self, name: str, role: str) -> None:
        self.name = name
        self.role = role

    @abstractmethod
    def run(self, context: AgentContext) -> Any:
        raise NotImplementedError
