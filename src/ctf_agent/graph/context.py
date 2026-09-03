"""Evidence-bounded context assembly for graph solver prompts."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ctf_agent.core.redaction import REDACTION, redact_string, redact_value
from ctf_agent.core.trace import summarize_text

DEFAULT_LIMITS = {
    "observations": 12,
    "experiments": 8,
    "memory": 5,
    "skills": 8,
    "experiment_assessments": 5,
    "replans": 3,
    "summary_chars": 1200,
}


class EvidenceFact(BaseModel):
    """A fact or constraint with explicit provenance."""

    model_config = ConfigDict(extra="forbid")

    source: str
    kind: str = "fact"
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    advisory: bool = False


class EvidenceObservation(BaseModel):
    """A bounded observation-like context item."""

    model_config = ConfigDict(extra="forbid")

    source: str
    kind: str = "observation"
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    advisory: bool = False


class EvidenceHypothesis(BaseModel):
    """A model-proposed hypothesis that must remain advisory."""

    model_config = ConfigDict(extra="forbid")

    source: str = "graph_state.hypotheses"
    claim: str
    confidence: float | None = None
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    advisory: bool = True


class EvidenceExperimentAssessment(BaseModel):
    """Bounded policy context for rejected or accepted experiment proposals."""

    model_config = ConfigDict(extra="forbid")

    fingerprint: dict[str, Any] = Field(default_factory=dict)
    action_type: str
    recommended_action: str
    duplicate: bool = False
    blocked_by_constraint: bool = False
    information_gain_score: float | None = None
    risk_penalty: float | None = None
    reasons: list[str] = Field(default_factory=list)
    missing_question: str | None = None
    iteration: int | None = None


class EvidencePacket(BaseModel):
    """Finite, redacted, JSON-serializable evidence for solver reasoning."""

    model_config = ConfigDict(extra="forbid")

    challenge: dict[str, Any]
    confirmed_facts: list[EvidenceFact] = Field(default_factory=list)
    constraints: list[EvidenceFact] = Field(default_factory=list)
    anomalies: list[EvidenceFact] = Field(default_factory=list)
    active_hypotheses: list[EvidenceHypothesis] = Field(default_factory=list)
    recent_experiments: list[EvidenceObservation] = Field(default_factory=list)
    recent_observations: list[EvidenceObservation] = Field(default_factory=list)
    memory_notes: list[EvidenceObservation] = Field(default_factory=list)
    skill_notes: list[EvidenceObservation] = Field(default_factory=list)
    tool_capabilities: list[dict[str, Any]] = Field(default_factory=list)
    network_authorization_scope: dict[str, Any] = Field(default_factory=dict)
    known_paths: list[str] = Field(default_factory=list)
    verified_candidates: list[dict[str, Any]] = Field(default_factory=list)
    recent_experiment_assessments: list[EvidenceExperimentAssessment] = Field(default_factory=list)
    replan_history: list[EvidenceExperimentAssessment] = Field(default_factory=list)
    prohibited_fingerprints: list[dict[str, Any]] = Field(default_factory=list)
    unanswered_questions: list[str] = Field(default_factory=list)
    context_budget: dict[str, Any] = Field(default_factory=dict)


def build_evidence_packet(
    workflow_state: Mapping[str, Any],
    *,
    challenge: Any,
    trace_events: Sequence[Any] | None,
    memory: Sequence[Any] | None,
    skills: Sequence[Any] | None,
    tools: Sequence[Any] | None,
    network_scope: Mapping[str, Any] | None,
    limits: Mapping[str, int] | None,
) -> EvidencePacket:
    """Build a bounded evidence packet without invoking tools or models."""

    budget = {**DEFAULT_LIMITS, **dict(limits or {})}
    summary_limit = max(80, int(budget["summary_chars"]))
    context_budget: dict[str, Any] = {"limits": dict(budget), "sections": {}}
    challenge_data = _challenge_data(challenge, summary_limit=summary_limit)
    packet = EvidencePacket(
        challenge=challenge_data,
        confirmed_facts=_confirmed_facts(workflow_state, challenge_data, summary_limit),
        constraints=_constraints(workflow_state, challenge_data, network_scope or {}, tools or [], summary_limit),
        anomalies=_anomalies(workflow_state, trace_events or [], summary_limit),
        active_hypotheses=_hypotheses(workflow_state, summary_limit),
        recent_experiments=_bounded_observations(
            workflow_state.get("experiments", []),
            section="experiments",
            max_items=int(budget["experiments"]),
            summary_limit=summary_limit,
            context_budget=context_budget,
            default_source="graph_state.experiments",
            default_kind="experiment",
        ),
        recent_observations=_bounded_observations(
            workflow_state.get("observations", []),
            section="observations",
            max_items=int(budget["observations"]),
            summary_limit=summary_limit,
            context_budget=context_budget,
            default_source="graph_state.observations",
            default_kind="observation",
        ),
        memory_notes=_bounded_observations(
            memory if memory is not None else workflow_state.get("memory_matches", []),
            section="memory",
            max_items=int(budget["memory"]),
            summary_limit=summary_limit,
            context_budget=context_budget,
            default_source="memory",
            default_kind="memory_note",
            advisory=True,
        ),
        skill_notes=_bounded_observations(
            skills if skills is not None else workflow_state.get("skill_notes", []),
            section="skills",
            max_items=int(budget["skills"]),
            summary_limit=summary_limit,
            context_budget=context_budget,
            default_source="skill",
            default_kind="skill_note",
            advisory=True,
        ),
        tool_capabilities=_json_safe(tools or [], summary_limit),
        network_authorization_scope=_json_safe(network_scope or {}, summary_limit),
        known_paths=_known_paths(workflow_state, challenge_data, summary_limit),
        verified_candidates=_verified_candidates(workflow_state, summary_limit),
        recent_experiment_assessments=_experiment_assessments(
            workflow_state,
            max_items=int(budget["experiment_assessments"]),
            summary_limit=summary_limit,
            context_budget=context_budget,
        ),
        replan_history=_replan_history(
            workflow_state,
            max_items=int(budget["replans"]),
            summary_limit=summary_limit,
            context_budget=context_budget,
        ),
        prohibited_fingerprints=_prohibited_fingerprints(workflow_state, summary_limit),
        unanswered_questions=_unanswered_questions(workflow_state, summary_limit),
        context_budget=context_budget,
    )
    return EvidencePacket.model_validate(packet.model_dump(mode="json"))


def _confirmed_facts(workflow_state: Mapping[str, Any], challenge_data: Mapping[str, Any], summary_limit: int) -> list[EvidenceFact]:
    facts: list[EvidenceFact] = []
    for item in _as_list(workflow_state.get("confirmed_facts")):
        facts.append(_fact(item, source="graph_state.confirmed_facts", kind="confirmed_fact", summary_limit=summary_limit))
    for key in ("id", "title", "category", "description", "files", "connection", "hints", "flag_regex"):
        value = challenge_data.get(key)
        if value not in (None, "", [], {}):
            facts.append(EvidenceFact(source=f"challenge.{key}", kind="challenge_metadata", summary=_summarize(value, summary_limit), data={"field": key, "value": _json_safe(value, summary_limit)}))
    metadata = challenge_data.get("metadata")
    if isinstance(metadata, Mapping):
        for key, value in sorted(metadata.items(), key=lambda item: str(item[0])):
            if value not in (None, "", [], {}):
                facts.append(EvidenceFact(source=f"challenge.metadata.{key}", kind="challenge_metadata", summary=_summarize(value, summary_limit), data={"field": str(key), "value": _json_safe(value, summary_limit)}))
    for item in _as_list(workflow_state.get("observations")):
        if _is_verifier_item(item):
            facts.append(_fact(item, source="graph_state.observations", kind="verifier_observation", summary_limit=summary_limit))
    for item in _as_list(workflow_state.get("artifacts")):
        facts.append(_fact(item, source="graph_state.artifacts", kind="artifact", summary_limit=summary_limit))
    for item in _verified_candidates(workflow_state, summary_limit):
        facts.append(EvidenceFact(source=str(item.get("source") or "graph_state.verified_candidates"), kind="verified_candidate", summary=_summarize(item, summary_limit), data=item))
    return facts


def _constraints(workflow_state: Mapping[str, Any], challenge_data: Mapping[str, Any], network_scope: Mapping[str, Any], tools: Sequence[Any], summary_limit: int) -> list[EvidenceFact]:
    constraints: list[EvidenceFact] = []
    for key in ("run_dir", "max_iterations"):
        value = workflow_state.get(key)
        if value not in (None, "", [], {}):
            constraints.append(EvidenceFact(source=f"graph_state.{key}", kind="runtime_constraint", summary=_summarize(value, summary_limit), data={"field": key, "value": _json_safe(value, summary_limit)}))
    if challenge_data.get("connection"):
        constraints.append(EvidenceFact(source="challenge.connection", kind="network_scope", summary=_summarize(challenge_data.get("connection"), summary_limit), data={"connection": _json_safe(challenge_data.get("connection"), summary_limit)}))
    if network_scope:
        constraints.append(EvidenceFact(source="network_authorization_scope", kind="network_scope", summary=_summarize(network_scope, summary_limit), data=_json_safe(network_scope, summary_limit)))
    if tools:
        constraints.append(EvidenceFact(source="tool_registry", kind="tool_limit", summary=_summarize([_tool_name(tool) for tool in tools], summary_limit), data={"tools": _json_safe(tools, summary_limit)}))
    for path in _known_paths(workflow_state, challenge_data, summary_limit):
        constraints.append(EvidenceFact(source="known_paths", kind="path_constraint", summary=path, data={"path": path}))
    return constraints


def _anomalies(workflow_state: Mapping[str, Any], trace_events: Sequence[Any], summary_limit: int) -> list[EvidenceFact]:
    anomalies: list[EvidenceFact] = []
    for item in _as_list(workflow_state.get("failed_actions")):
        anomalies.append(_fact(item, source="graph_state.failed_actions", kind="failed_action", summary_limit=summary_limit))
    for item in _as_list(workflow_state.get("observations")):
        if _item_flag(item, {"anomaly", "unexpected", "error", "failed", "failure"}):
            anomalies.append(_fact(item, source="graph_state.observations", kind="anomaly", summary_limit=summary_limit))
    for event in trace_events:
        data = _event_data(event)
        if data.get("exit_code") not in (None, 0) or data.get("stderr"):
            anomalies.append(_fact(data, source="trace_events", kind="trace_anomaly", summary_limit=summary_limit))
    failure_reason = workflow_state.get("failure_reason")
    if failure_reason:
        anomalies.append(EvidenceFact(source="graph_state.failure_reason", kind="failure_reason", summary=_summarize(failure_reason, summary_limit)))
    return anomalies


def _hypotheses(workflow_state: Mapping[str, Any], summary_limit: int) -> list[EvidenceHypothesis]:
    hypotheses: list[EvidenceHypothesis] = []
    for item in _as_list(workflow_state.get("hypotheses")):
        data = _mapping(item)
        claim = str(data.get("claim") or data.get("hypothesis") or data.get("summary") or _summarize(data, summary_limit))
        hypotheses.append(EvidenceHypothesis(source=str(data.get("source") or "graph_state.hypotheses"), claim=_summarize(claim, summary_limit), confidence=_confidence(data.get("confidence")), evidence_for=[_summarize(value, summary_limit) for value in _as_list(data.get("evidence_for"))], evidence_against=[_summarize(value, summary_limit) for value in _as_list(data.get("evidence_against"))], data=_json_safe(data, summary_limit), advisory=True))
    current = workflow_state.get("current_hypothesis")
    if current:
        hypotheses.append(EvidenceHypothesis(source="graph_state.current_hypothesis", claim=_summarize(current, summary_limit), advisory=True))
    return hypotheses


def _bounded_observations(items: Sequence[Any], *, section: str, max_items: int, summary_limit: int, context_budget: dict[str, Any], default_source: str, default_kind: str, advisory: bool = False) -> list[EvidenceObservation]:
    values = _as_list(items)
    included = values[-max(0, max_items):] if max_items > 0 else []
    result: list[EvidenceObservation] = []
    truncated_items = 0
    for item in included:
        data = _mapping(item)
        summary = _item_summary(data if data else item, summary_limit)
        if "... <truncated " in summary:
            truncated_items += 1
        result.append(EvidenceObservation(source=str(data.get("source") or data.get("source_run") or data.get("tool") or default_source), kind=str(data.get("kind") or data.get("type") or default_kind), summary=summary, data=_json_safe(data if data else item, summary_limit), advisory=advisory))
    context_budget["sections"][section] = {"original_count": len(values), "included_count": len(result), "omitted_count": max(0, len(values) - len(result)), "truncated_items": truncated_items, "summary_chars": summary_limit}
    return result


def _experiment_assessments(workflow_state: Mapping[str, Any], *, max_items: int, summary_limit: int, context_budget: dict[str, Any]) -> list[EvidenceExperimentAssessment]:
    values = _as_list(workflow_state.get("experiment_assessments"))
    included = values[-max(0, max_items):] if max_items > 0 else []
    result = [_assessment(item, summary_limit) for item in included]
    context_budget["sections"]["experiment_assessments"] = {
        "original_count": len(values),
        "included_count": len(result),
        "omitted_count": max(0, len(values) - len(result)),
        "summary_chars": summary_limit,
    }
    return result


def _replan_history(workflow_state: Mapping[str, Any], *, max_items: int, summary_limit: int, context_budget: dict[str, Any]) -> list[EvidenceExperimentAssessment]:
    replans = [
        item for item in _as_list(workflow_state.get("experiment_assessments"))
        if _mapping(item).get("recommended_action") in {"replan", "pause"}
    ]
    observations = [
        item for item in _as_list(workflow_state.get("observations"))
        if _mapping(item).get("source") == "experiment_policy" and _mapping(item).get("recommended_action") in {"replan", "pause"}
    ]
    merged = replans + observations
    included = merged[-max(0, max_items):] if max_items > 0 else []
    result = [_assessment(item, summary_limit) for item in included]
    context_budget["sections"]["replan_history"] = {
        "original_count": len(merged),
        "included_count": len(result),
        "omitted_count": max(0, len(merged) - len(result)),
        "summary_chars": summary_limit,
    }
    return result


def _prohibited_fingerprints(workflow_state: Mapping[str, Any], summary_limit: int) -> list[dict[str, Any]]:
    prohibited: list[dict[str, Any]] = []
    for item in _as_list(workflow_state.get("experiment_assessments")):
        data = _mapping(item)
        if data.get("recommended_action") not in {"replan", "pause"}:
            continue
        fingerprint = _fingerprint_summary(data.get("fingerprint"), summary_limit)
        if fingerprint:
            prohibited.append({
                **fingerprint,
                "reason": _summarize(data.get("reasons") or data.get("missing_question") or data.get("recommended_action"), summary_limit),
                "iteration": _int_or_none(data.get("iteration")),
            })
    return _dedupe_dicts(prohibited)[-10:]


def _unanswered_questions(workflow_state: Mapping[str, Any], summary_limit: int) -> list[str]:
    questions: list[str] = []
    for item in _as_list(workflow_state.get("experiment_assessments")) + _as_list(workflow_state.get("observations")):
        data = _mapping(item)
        for key in ("missing_question", "failure_signal", "pending_human_question"):
            value = data.get(key)
            if value not in (None, "", [], {}):
                questions.append(_summarize(value, summary_limit))
        for reason in _as_list(data.get("reasons")):
            if any(word in str(reason).lower() for word in ("missing", "unknown", "unanswered", "requires human", "confirm")):
                questions.append(_summarize(reason, summary_limit))
    questions.extend(_summarize(item, summary_limit) for item in _as_list(workflow_state.get("unknowns")))
    if workflow_state.get("pending_human_question"):
        questions.append(_summarize(workflow_state.get("pending_human_question"), summary_limit))
    return _dedupe_strings(questions)[:20]


def _assessment(item: Any, summary_limit: int) -> EvidenceExperimentAssessment:
    data = _mapping(item)
    return EvidenceExperimentAssessment(
        fingerprint=_fingerprint_summary(data.get("fingerprint") or data, summary_limit),
        action_type=str(data.get("action_type") or _mapping(data.get("fingerprint")).get("action_type") or "unknown"),
        recommended_action=str(data.get("recommended_action") or "unknown"),
        duplicate=bool(data.get("duplicate")),
        blocked_by_constraint=bool(data.get("blocked_by_constraint")),
        information_gain_score=_float_or_none(data.get("information_gain_score")),
        risk_penalty=_float_or_none(data.get("risk_penalty")),
        reasons=[_summarize(reason, summary_limit) for reason in _as_list(data.get("reasons"))[:5]],
        missing_question=_summarize(data.get("missing_question"), summary_limit) if data.get("missing_question") else None,
        iteration=_int_or_none(data.get("iteration")),
    )


def _fingerprint_summary(value: Any, summary_limit: int) -> dict[str, Any]:
    data = _mapping(value)
    if not data:
        return {}
    return {
        "action_type": str(data.get("action_type") or "unknown"),
        "digest": _summarize(data.get("digest") or "", summary_limit),
    }


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _dedupe_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        key = json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _known_paths(workflow_state: Mapping[str, Any], challenge_data: Mapping[str, Any], summary_limit: int) -> list[str]:
    paths: set[str] = set()
    for item in _as_list(challenge_data.get("files")):
        paths.add(_summarize(item, summary_limit))
    for item in _as_list(workflow_state.get("artifacts")):
        data = _mapping(item)
        for key in ("path", "file", "location"):
            if data.get(key):
                paths.add(_summarize(data[key], summary_limit))
    run_dir = workflow_state.get("run_dir")
    if run_dir:
        paths.add(_summarize(run_dir, summary_limit))
    return sorted(paths)


def _verified_candidates(workflow_state: Mapping[str, Any], summary_limit: int) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    for item in _as_list(workflow_state.get("verified_candidates")):
        data = _mapping(item)
        if not data.get("verified"):
            continue
        if not data.get("value") and not data.get("candidate"):
            continue
        safe = _json_safe(data, summary_limit)
        if "value" in safe:
            safe["value"] = _summarize(safe["value"], summary_limit)
        if "candidate" in safe:
            safe["candidate"] = _summarize(safe["candidate"], summary_limit)
        verified.append(safe)
    return verified


def _fact(item: Any, *, source: str, kind: str, summary_limit: int) -> EvidenceFact:
    data = _mapping(item)
    return EvidenceFact(source=str(data.get("source") or source), kind=str(data.get("kind") or data.get("type") or kind), summary=_item_summary(data if data else item, summary_limit), data=_json_safe(data if data else item, summary_limit), advisory=bool(data.get("advisory", False)) if data else False)


def _item_summary(item: Any, summary_limit: int) -> str:
    data = _mapping(item)
    for key in ("summary", "result_summary", "description", "title", "message", "value", "text", "raw"):
        value = data.get(key)
        if value not in (None, "", [], {}):
            return _summarize(value, summary_limit)
    return _summarize(item, summary_limit)


def _challenge_data(challenge: Any, *, summary_limit: int) -> dict[str, Any]:
    data = challenge.to_dict() if hasattr(challenge, "to_dict") else _mapping(challenge)
    safe = _json_safe(data, summary_limit)
    for key in ("description", "connection", "flag_regex"):
        if key in safe and isinstance(safe[key], str):
            safe[key] = _summarize(safe[key], summary_limit)
    if isinstance(safe.get("hints"), list):
        safe["hints"] = [_summarize(item, summary_limit) for item in safe["hints"]]
    return safe


def _event_data(event: Any) -> dict[str, Any]:
    return _mapping(event.to_dict()) if hasattr(event, "to_dict") else _mapping(event)


def _json_safe(value: Any, summary_limit: int | None = None) -> Any:
    redacted = redact_value(_plain_value(value))
    limited = _limit_strings(redacted, summary_limit) if summary_limit is not None else redacted
    return json.loads(json.dumps(limited, ensure_ascii=False, default=str))


def _limit_strings(value: Any, summary_limit: int) -> Any:
    if isinstance(value, str):
        return summarize_text(_sanitize_context_text(value), limit=summary_limit) or ""
    if isinstance(value, Mapping):
        return {str(key): _limit_strings(item, summary_limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_limit_strings(item, summary_limit) for item in value]
    if isinstance(value, tuple):
        return [_limit_strings(item, summary_limit) for item in value]
    return value


def _plain_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _plain_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain_value(item) for item in value]
    return value


def _sanitize_context_text(value: str) -> str:
    text = redact_string(value)
    text = text.replace("Authorization", REDACTION).replace("authorization", REDACTION)
    text = text.replace("Bearer", REDACTION).replace("bearer", REDACTION)
    text = text.replace("api_key", REDACTION).replace("API_KEY", REDACTION)
    return text


def _summarize(value: Any, summary_limit: int) -> str:
    safe = _json_safe(value)
    text = safe if isinstance(safe, str) else json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str)
    return summarize_text(_sanitize_context_text(text), limit=summary_limit) or ""


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if hasattr(value, "to_dict"):
        return _mapping(value.to_dict())
    if is_dataclass(value):
        return _mapping(asdict(value))
    return {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _confidence(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_verifier_item(item: Any) -> bool:
    data = _mapping(item)
    source = str(data.get("source") or data.get("agent") or data.get("kind") or "").lower()
    return "verifier" in source or data.get("verified") is True


def _item_flag(item: Any, words: set[str]) -> bool:
    data = _mapping(item)
    haystack = " ".join(str(data.get(key, "")) for key in ("kind", "type", "status", "summary", "message")).lower()
    return any(word in haystack for word in words)


def _tool_name(tool: Any) -> str:
    data = _mapping(tool)
    return str(data.get("name") or data.get("tool") or tool)
