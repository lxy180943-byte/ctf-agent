"""Bridge LangGraph single-argument reasoner hook to PydanticAI."""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence

from ctf_agent.core.redaction import REDACTION, is_sensitive_key, redact_string, redact_value
from ctf_agent.core.trace import summarize_text
from ctf_agent.graph.context import build_evidence_packet
from ctf_agent.pydantic_agent.agent import PydanticAISolverReasoner, ReasoningError, SolverDependencies
from ctf_agent.pydantic_agent.models import SolverDecision

EvidenceProvider = Sequence[Mapping[str, Any]] | Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any]]]
_DEPENDENCY_TEXT_LIMIT = 1200


class GraphReasonerAdapter:
    """Adapt ``reasoner(graph_state)`` without granting the reasoner tool access."""

    def __init__(self, reasoner: PydanticAISolverReasoner, *, challenge: Mapping[str, Any], memory: EvidenceProvider = (), skills: EvidenceProvider = (), tool_capabilities: Sequence[Mapping[str, Any]] = (), network_authorization_scope: Mapping[str, Any] = (), run_id: str = "", provider_name: str = "", model_name: str = "", iteration_limits: Mapping[str, int] = (), trace_summary: EvidenceProvider = ()) -> None:
        self.reasoner = reasoner
        self.challenge = dict(challenge)
        self.memory, self.skills, self.trace_summary = memory, skills, trace_summary
        self.tool_capabilities = [dict(item) for item in tool_capabilities]
        self.network_authorization_scope = dict(network_authorization_scope)
        self.run_id, self.provider_name, self.model_name = run_id, provider_name, model_name
        self.iteration_limits = dict(iteration_limits)
        self.last_dependencies: SolverDependencies | None = None

    def __call__(self, graph_state: Mapping[str, Any]) -> SolverDecision:
        packet = build_evidence_packet(
            graph_state,
            challenge=self.challenge,
            trace_events=self._items(self.trace_summary, graph_state),
            memory=self._items(self.memory, graph_state),
            skills=self._items(self.skills, graph_state),
            tools=self.tool_capabilities,
            network_scope=self.network_authorization_scope,
            limits=self.iteration_limits,
        )
        evidence_packet = _dependency_value(packet.model_dump(mode="json"))
        snapshot = _minimal_snapshot(graph_state, evidence_packet)
        deps = SolverDependencies(
            challenge=evidence_packet.get("challenge", {}),
            evidence_packet=evidence_packet,
            graph_state_snapshot=snapshot,
            recent_observations=list(evidence_packet.get("recent_observations", [])),
            recent_trace_summary=[],
            memory_matches=list(evidence_packet.get("memory_notes", [])),
            skill_notes=list(evidence_packet.get("skill_notes", [])),
            tool_capabilities=list(evidence_packet.get("tool_capabilities", [])),
            network_authorization_scope=dict(evidence_packet.get("network_authorization_scope", {})),
            iteration_limits=dict(_dependency_value(self.iteration_limits)),
            run_id=str(_dependency_value(self.run_id)),
            provider_name=str(_dependency_value(self.provider_name)),
            model_name=str(_dependency_value(self.model_name)),
        )
        self.last_dependencies = deps
        try:
            return self.reasoner.reason(snapshot, deps)
        except ReasoningError as exc:
            raise ReasoningError("graph_reasoning_failed", "Graph reasoning failed safely.") from exc

    @staticmethod
    def _items(provider: EvidenceProvider, state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        values = provider(state) if callable(provider) else provider
        items: list[Mapping[str, Any]] = []
        for item in values:
            if isinstance(item, Mapping):
                items.append(dict(redact_value(item)))
            else:
                items.append({"value": redact_value(item)})
        return items


def _minimal_snapshot(graph_state: Mapping[str, Any], evidence_packet: Mapping[str, Any]) -> dict[str, Any]:
    count_fields = {
        "confirmed_facts": "confirmed_fact_count",
        "hypotheses": "hypothesis_count",
        "candidate_chains": "candidate_chain_count",
        "experiments": "experiment_count",
        "observations": "observation_count",
        "events": "event_count",
        "artifacts": "artifact_count",
        "tool_calls": "tool_call_count",
        "failed_actions": "failed_action_count",
        "verified_candidates": "verified_candidate_count",
    }
    counts = {target: len(graph_state.get(source, []) or []) for source, target in count_fields.items()}
    summary = {
        "phase": graph_state.get("phase"),
        "iteration": graph_state.get("iteration"),
        "max_iterations": graph_state.get("max_iterations"),
        "paused": graph_state.get("paused"),
        "solved": graph_state.get("solved"),
        "failure_reason": graph_state.get("failure_reason"),
        "pause_reason": graph_state.get("pause_reason"),
        "pending_human_question": graph_state.get("pending_human_question"),
        "next_goal": graph_state.get("next_goal"),
        "current_hypothesis": graph_state.get("current_hypothesis"),
        "known_paths": evidence_packet.get("known_paths", []),
        "verified_candidates": evidence_packet.get("verified_candidates", []),
        "counts": counts,
    }
    return dict(_dependency_value(summary))


_ALLOWED_SENSITIVE_NAMED_CONTAINERS = {"network_authorization_scope"}


def _drop_dependency_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return normalized not in _ALLOWED_SENSITIVE_NAMED_CONTAINERS and is_sensitive_key(key)


def _dependency_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _dependency_value(item)
            for key, item in value.items()
            if not _drop_dependency_key(key)
        }
    if isinstance(value, list):
        return [_dependency_value(item) for item in value]
    if isinstance(value, tuple):
        return [_dependency_value(item) for item in value]
    if isinstance(value, str):
        text = redact_string(value)
        text = re.sub(r"(?i)authorization\s*[:=]\s*(?:(?:bearer|token)\s*)?<redacted>", REDACTION, text)
        text = re.sub(r"(?i)\b(?:bearer|token)\s+<redacted>", REDACTION, text)
        text = re.sub(r"(?i)\bauthorization\b", REDACTION, text)
        text = re.sub(r"(?i)\bbearer\b", REDACTION, text)
        text = re.sub(r"(?i)api[_ -]?key", REDACTION, text)
        return summarize_text(text, limit=_DEPENDENCY_TEXT_LIMIT) or ""
    return value
