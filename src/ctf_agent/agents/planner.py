from __future__ import annotations

import shlex
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from ctf_agent.agents.base import Agent, AgentContext
from ctf_agent.core.config import get_nested
from ctf_agent.core.trace import TraceEvent
from ctf_agent.llm import LLMMessage
from ctf_agent.llm.actions import extract_command_actions, parse_json_object
from ctf_agent.memory import MemoryStore


@dataclass
class PlanCommand:
    command: str
    reason: str
    timeout: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "reason": self.reason,
            "timeout": self.timeout,
            "metadata": dict(self.metadata),
        }


@dataclass
class Plan:
    rationale: str
    commands: list[PlanCommand] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rationale": self.rationale,
            "commands": [command.to_dict() for command in self.commands],
            "metadata": dict(self.metadata),
        }


class PlannerAgent(Agent):
    def __init__(self) -> None:
        super().__init__(name="planner", role="Create a deterministic MVP plan from challenge metadata and available tools.")

    def run(self, context: AgentContext) -> Plan:
        memory_matches = self._memory_matches(context)
        context.metadata["memory_matches"] = memory_matches
        plan = self._llm_plan(context) if context.llm_provider and context.prompt_store else None
        if plan is None:
            plan = self._deterministic_plan(context)
        plan.metadata["memory_matches"] = memory_matches
        if memory_matches:
            plan.rationale = f"{plan.rationale} Recalled {len(memory_matches)} prior knowledge item(s)."
        context.trace_store.append(
            TraceEvent(
                challenge_id=context.state.challenge.id,
                agent=self.name,
                action="plan",
                stdout=plan.rationale,
                metadata={"plan": plan.to_dict()},
            )
        )
        return plan

    def _memory_matches(self, context: AgentContext) -> list[dict[str, Any]]:
        if not isinstance(context.config.get("memory"), dict) or get_nested(context.config, ("memory", "enabled")) is False:
            return []
        try:
            store = MemoryStore.from_config(context.config)
            challenge = context.state.challenge
            query = " ".join([challenge.title, challenge.category, challenge.description, " ".join(challenge.hints), " ".join(challenge.files)])
            limit = int(get_nested(context.config, ("memory", "search_limit")) or 5)
            file_magic = _file_magic_hints(context)
            available_tools = _available_tool_names(context)
            items = store.search(
                query,
                category=challenge.category if challenge.category else None,
                limit=limit,
                file_magic=file_magic,
                available_tools=available_tools,
            )
            store.mark_used(item.id for item in items)
        except Exception as exc:
            context.trace_store.append(
                TraceEvent(
                    challenge_id=context.state.challenge.id,
                    agent=self.name,
                    action="memory-search-failed",
                    stderr=str(exc),
                    metadata={"reason": "Memory retrieval failed; continuing without prior knowledge"},
                )
            )
            return []
        matches = [item.to_dict() for item in items]
        if matches:
            context.trace_store.append(
                TraceEvent(
                    challenge_id=context.state.challenge.id,
                    agent=self.name,
                    action="memory-search",
                    stdout=f"matched {len(matches)} knowledge item(s)",
                    metadata={"matches": matches},
                )
            )
        return matches

    def _llm_plan(self, context: AgentContext) -> Plan | None:
        challenge = context.state.challenge
        assert context.llm_provider is not None
        assert context.prompt_store is not None
        tools = [tool.to_dict() for tool in context.tool_registry.recommend(challenge.category, limit=8)]
        memory_matches = context.metadata.get("memory_matches") or []
        prompt = context.prompt_store.render(
            "planner",
            {
                "challenge_json": json.dumps(challenge.to_dict(), ensure_ascii=False, sort_keys=True),
                "tools_json": json.dumps(tools, ensure_ascii=False, sort_keys=True),
                "memory_json": json.dumps(memory_matches, ensure_ascii=False, sort_keys=True),
                "observed_paths_json": json.dumps(challenge.files, ensure_ascii=False, sort_keys=True),
                "brain_context_json": json.dumps(context.metadata.get("brain_context") or {}, ensure_ascii=False, sort_keys=True),
                "relevant_skill_notes_json": json.dumps(context.metadata.get("relevant_skill_notes") or [], ensure_ascii=False, sort_keys=True),
                "structured_observations_json": "[]",
                "observations_json": "[]",
                "php_analysis_json": "[]",
                "trace_json": "[]",
                "flag_candidates_json": json.dumps([candidate.to_dict() for candidate in context.state.flag_candidates], ensure_ascii=False, sort_keys=True),
            },
        )
        try:
            response = context.llm_provider.complete(
                messages=[
                    LLMMessage(role="system", content="Return only structured JSON actions for the CTF planner."),
                    LLMMessage(role="user", content=prompt),
                ],
                response_format="json_object",
            )
            data = parse_json_object(response.content)
            actions = extract_command_actions(data, max_actions=min(context.max_steps, 3))
        except Exception as exc:
            context.trace_store.append(
                TraceEvent(
                    challenge_id=challenge.id,
                    agent=self.name,
                    action="llm-plan-fallback",
                    stderr=str(exc),
                    metadata={"reason": "LLM planner failed; using deterministic fallback"},
                )
            )
            return None

        commands = [
            PlanCommand(
                command=action["command"],
                reason=action["reason"],
                timeout=int(action["timeout"]) if action.get("timeout") is not None else context.timeout,
                metadata={"source": "llm", **action.get("metadata", {})},
            )
            for action in actions
        ]
        if not commands:
            return None
        return Plan(
            rationale=str(data.get("rationale") or "LLM planner produced structured command actions."),
            commands=commands[: context.max_steps],
            metadata={"source": "llm", "provider": context.llm_provider.name, "raw": data},
        )

    def _deterministic_plan(self, context: AgentContext) -> Plan:
        challenge = context.state.challenge
        commands: list[PlanCommand] = []
        recommended = [tool.name for tool in context.tool_registry.recommend(challenge.category, limit=5)]

        for file_name in challenge.files:
            code = (
                "from pathlib import Path; "
                f"p=Path({file_name!r}); "
                "data=p.read_bytes(); "
                "print(data[:200000].decode('utf-8','replace'))"
            )
            commands.append(
                PlanCommand(
                    command="python3 -c " + shlex.quote(code),
                    reason=f"Read printable content from {file_name} using Python so the MVP works in minimal Docker images.",
                    timeout=context.timeout,
                    metadata={"file": file_name, "strategy": "text-scan"},
                )
            )

        if challenge.description:
            code = f"print({challenge.description!r})"
            commands.insert(
                0,
                PlanCommand(
                    command="python3 -c " + shlex.quote(code),
                    reason="Echo challenge description into the observation stream for verifier regex scanning.",
                    timeout=min(context.timeout, 10),
                    metadata={"strategy": "description-scan"},
                ),
            )

        plan = Plan(
            rationale=(
                f"Deterministic MVP plan for category={challenge.category}; "
                f"recommended tools={recommended or ['generic']}; files={challenge.files}; "
                f"memory_matches={len(context.metadata.get('memory_matches') or [])}."
            ),
            commands=commands[: context.max_steps],
            metadata={"recommended_tools": recommended, "source": "deterministic"},
        )
        return plan


def _file_magic_hints(context: AgentContext) -> list[str]:
    hints: list[str] = []
    for file_name in context.state.challenge.files:
        path = context.layout.work_dir / file_name
        suffix = Path(file_name).suffix.lower()
        if suffix:
            hints.append(suffix)
        try:
            data = path.read_bytes()[:8]
        except OSError:
            continue
        if data.startswith(b"\x7fELF"):
            hints.append("ELF")
        elif data.startswith(b"MZ"):
            hints.append("PE")
        elif data.startswith(b"PK\x03\x04"):
            hints.append("zip")
        elif data.startswith(b"\x89PNG"):
            hints.append("PNG")
        elif data.startswith(b"\xff\xd8\xff"):
            hints.append("JPEG")
        elif data[:4] == b"\xa7\r\r\n":
            hints.append("python bytecode")
    return hints


def _available_tool_names(context: AgentContext) -> list[str]:
    names: list[str] = []
    for tool in context.tool_registry.list():
        if not tool.required_bins:
            names.append(tool.name)
            continue
        if all(__import__("shutil").which(binary) for binary in tool.required_bins):
            names.append(tool.name)
            names.extend(binary for binary in tool.required_bins if binary not in names)
    return names
