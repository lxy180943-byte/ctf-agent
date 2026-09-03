"""LangGraph-compatible transient state for an authorized CTF workflow.

ChallengeRunState and TraceStore remain the persisted production contracts. This
module provides the graph projection used by future LangGraph nodes and can be
serialized into a resume artifact without provider secrets.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Mapping, TypedDict

from ctf_agent.core.models import Challenge


def append_items(current: list[dict[str, Any]] | None, update: object) -> list[dict[str, Any]]:
    """Reducer used by StateGraph for append-only evidence streams."""

    result = list(current or [])
    if update is None:
        return result
    items = update if isinstance(update, list) else [update]
    for item in items:
        if isinstance(item, Mapping):
            result.append({str(key): _json_value(value) for key, value in item.items()})
        else:
            result.append({"value": _json_value(item)})
    return result


def append_evidence_items(current: list[dict[str, Any]] | None, update: object) -> list[dict[str, Any]]:
    """Append evidence while deduplicating stable content and merging provenance."""

    result: list[dict[str, Any]] = []
    positions: dict[str, int] = {}

    def add(item: object) -> None:
        normalized = _evidence_item(item)
        key = _evidence_key(normalized)
        if key in positions:
            result[positions[key]] = _merge_provenance(result[positions[key]], normalized)
            return
        positions[key] = len(result)
        result.append(normalized)

    for item in current or []:
        add(item)
    if update is None:
        return result
    items = update if isinstance(update, list) else [update]
    for item in items:
        add(item)
    return result


def merge_experiment_items(current: list[dict[str, Any]] | None, update: object) -> list[dict[str, Any]]:
    """Reducer for experiment history: append new ids, merge completion updates."""

    result: list[dict[str, Any]] = []
    positions: dict[str, int] = {}

    def add(item: object) -> None:
        normalized = _evidence_item(item)
        experiment_id = str(normalized.get("id") or "").strip()
        if experiment_id and experiment_id in positions:
            existing = result[positions[experiment_id]]
            result[positions[experiment_id]] = _merge_experiment(existing, normalized)
            return
        if experiment_id:
            positions[experiment_id] = len(result)
        result.append(normalized)

    for item in current or []:
        add(item)
    if update is None:
        return result
    items = update if isinstance(update, list) else [update]
    for item in items:
        add(item)
    return result


@dataclass(frozen=True)
class StateFieldPolicy:
    """Documents field provenance and whether the model may propose updates."""

    source: str
    model_writable: bool
    description: str


STATE_FIELD_POLICIES: dict[str, StateFieldPolicy] = {
    "challenge": StateFieldPolicy("platform adapter", False, "Normalized challenge metadata."),
    "run_dir": StateFieldPolicy("WorkspaceManager", False, "Absolute local run directory."),
    "phase": StateFieldPolicy("graph runtime", False, "Current workflow phase."),
    "confirmed_facts": StateFieldPolicy("evidence pipeline", False, "Facts backed by observations."),
    "constraints": StateFieldPolicy("evidence pipeline", False, "Verified limits and structural constraints backed by observations."),
    "anomalies": StateFieldPolicy("evidence pipeline", False, "Observed failures, blocked actions, and expectation mismatches."),
    "evidence_deltas": StateFieldPolicy("evidence pipeline", False, "Append-only extracted evidence deltas with provenance."),
    "unknowns": StateFieldPolicy("reasoning model", True, "Open questions to reduce."),
    "hypotheses": StateFieldPolicy("reasoning model", True, "Candidate explanations."),
    "current_hypothesis": StateFieldPolicy("reasoning model", True, "Hypothesis being tested."),
    "candidate_chains": StateFieldPolicy("reasoning model", True, "Ordered candidate solution routes."),
    "hypothesis_updates": StateFieldPolicy("evidence reconciliation", False, "Append-only audit of deterministic hypothesis and chain updates."),
    "selected_experiment": StateFieldPolicy("experiment policy", False, "Last policy-approved experiment selected for execution."),
    "last_experiment_assessment": StateFieldPolicy("experiment policy", False, "Most recent deterministic experiment policy assessment."),
    "experiment_assessments": StateFieldPolicy("experiment policy", False, "Append-only experiment policy assessment audit."),
    "replan_required": StateFieldPolicy("experiment policy", False, "True when the selected experiment was rejected and reasoning must replan."),
    "consecutive_replans": StateFieldPolicy("experiment policy", False, "Consecutive policy replan count for loop prevention."),
    "experiments": StateFieldPolicy("graph executor", False, "Planned or completed guarded experiments."),
    "observations": StateFieldPolicy("executor and summarizer", False, "Append-only structured evidence."),
    "events": StateFieldPolicy("graph runtime", False, "Append-only workflow audit events."),
    "artifacts": StateFieldPolicy("executor", False, "Append-only artifact descriptors."),
    "tool_calls": StateFieldPolicy("guarded tool gateway", False, "Append-only executed tool call records."),
    "failed_actions": StateFieldPolicy("guarded tool gateway", False, "Rejected or failed action records."),
    "verified_candidates": StateFieldPolicy("VerifierAgent", False, "Only verifier-confirmed candidates."),
    "next_goal": StateFieldPolicy("reasoning model", True, "Next bounded objective."),
    "iteration": StateFieldPolicy("graph runtime", False, "Completed reasoning iteration count."),
    "pause_reason": StateFieldPolicy("pause action", False, "Reason the workflow requires human input."),
    "pending_human_question": StateFieldPolicy("pause action", False, "Specific requested human input."),
    "max_iterations": StateFieldPolicy("orchestrator configuration", False, "Hard iteration budget."),
    "paused": StateFieldPolicy("reasoning model via pause action", True, "Human-review pause request."),
    "solved": StateFieldPolicy("VerifierAgent", False, "True only after verification."),
    "failure_reason": StateFieldPolicy("graph runtime", False, "Terminal failure explanation."),
    "human_decisions": StateFieldPolicy("UI or CLI user", False, "Append-only human approvals and notes."),
    "memory_matches": StateFieldPolicy("MemoryStore", False, "Retrieved traceable memory items."),
    "skill_notes": StateFieldPolicy("SkillIndex", False, "Retrieved bounded skill excerpts."),
}

MODEL_WRITABLE_FIELDS = frozenset(name for name, policy in STATE_FIELD_POLICIES.items() if policy.model_writable)


class WorkflowState(TypedDict):
    """Complete graph state. Comments and policies state source/model authority."""

    challenge: dict[str, Any]  # platform adapter; model cannot modify
    run_dir: str  # WorkspaceManager; model cannot modify
    phase: str  # graph runtime; model cannot modify
    confirmed_facts: Annotated[list[dict[str, Any]], append_evidence_items]  # evidence pipeline; model cannot modify
    constraints: Annotated[list[dict[str, Any]], append_evidence_items]  # evidence pipeline; model cannot modify
    anomalies: Annotated[list[dict[str, Any]], append_evidence_items]  # evidence pipeline; model cannot modify
    evidence_deltas: Annotated[list[dict[str, Any]], append_evidence_items]  # evidence pipeline; model cannot modify
    unknowns: list[str]  # reasoning model may modify
    hypotheses: list[dict[str, Any]]  # reasoning model may modify
    current_hypothesis: str | None  # reasoning model may modify
    candidate_chains: list[dict[str, Any]]  # reasoning model may modify
    hypothesis_updates: Annotated[list[dict[str, Any]], append_items]  # evidence reconciliation; append-only
    selected_experiment: dict[str, Any] | None  # experiment policy; model cannot modify
    last_experiment_assessment: dict[str, Any] | None  # experiment policy; model cannot modify
    experiment_assessments: Annotated[list[dict[str, Any]], append_items]  # experiment policy; append-only
    replan_required: bool  # experiment policy; model cannot modify
    consecutive_replans: int  # experiment policy; model cannot modify
    experiments: Annotated[list[dict[str, Any]], merge_experiment_items]  # executor; append/merge by experiment id
    observations: Annotated[list[dict[str, Any]], append_items]  # summarizer; append-only
    events: Annotated[list[dict[str, Any]], append_items]  # runtime; append-only
    artifacts: Annotated[list[dict[str, Any]], append_items]  # executor; model cannot modify
    tool_calls: Annotated[list[dict[str, Any]], append_items]  # gateway; append-only
    failed_actions: Annotated[list[dict[str, Any]], append_items]  # gateway; append-only
    verified_candidates: Annotated[list[dict[str, Any]], append_items]  # verifier only
    next_goal: str | None  # reasoning model may modify
    iteration: int  # graph runtime; model cannot modify
    max_iterations: int  # orchestrator config; model cannot modify
    paused: bool  # model may request only through pause action
    solved: bool  # verifier only
    pause_reason: str | None  # pause action; model cannot modify directly
    pending_human_question: str | None  # pause action; model cannot modify directly
    failure_reason: str | None  # graph runtime; model cannot modify
    human_decisions: Annotated[list[dict[str, Any]], append_items]  # UI or CLI; append-only
    memory_matches: list[dict[str, Any]]  # MemoryStore; model cannot modify
    skill_notes: list[dict[str, Any]]  # SkillIndex; model cannot modify


def initial_workflow_state(
    challenge: Challenge | Mapping[str, Any],
    *,
    run_dir: str | Path,
    max_iterations: int = 20,
    memory_matches: list[Mapping[str, Any]] | None = None,
    skill_notes: list[Mapping[str, Any]] | None = None,
) -> WorkflowState:
    """Create a complete, JSON-safe state for a new graph invocation."""

    if max_iterations < 1:
        raise ValueError("max_iterations must be at least 1")
    challenge_data = challenge.to_dict() if isinstance(challenge, Challenge) else dict(challenge)
    if not challenge_data.get("id"):
        raise ValueError("challenge must include a non-empty id")
    return {
        "challenge": _json_value(challenge_data),
        "run_dir": str(Path(run_dir).expanduser()),
        "phase": "initialize",
        "confirmed_facts": [],
        "constraints": [],
        "anomalies": [],
        "evidence_deltas": [],
        "unknowns": [],
        "hypotheses": [],
        "current_hypothesis": None,
        "candidate_chains": [],
        "hypothesis_updates": [],
        "selected_experiment": None,
        "last_experiment_assessment": None,
        "experiment_assessments": [],
        "replan_required": False,
        "consecutive_replans": 0,
        "experiments": [],
        "observations": [],
        "events": [],
        "artifacts": [],
        "tool_calls": [],
        "failed_actions": [],
        "verified_candidates": [],
        "next_goal": None,
        "iteration": 0,
        "max_iterations": max_iterations,
        "paused": False,
        "solved": False,
        "pause_reason": None,
        "pending_human_question": None,
        "failure_reason": None,
        "human_decisions": [],
        "memory_matches": append_items([], memory_matches or []),
        "skill_notes": append_items([], skill_notes or []),
    }


def serialize_workflow_state(state: WorkflowState) -> dict[str, Any]:
    """Return a JSON-safe resume payload without non-serializable runtime objects."""

    return {name: _json_value(state[name]) for name in STATE_FIELD_POLICIES if name in state}


def workflow_state_to_json(state: WorkflowState) -> str:
    """Encode graph state for a workspace resume artifact."""

    return json.dumps(serialize_workflow_state(state), ensure_ascii=False, sort_keys=True)


def apply_model_update(state: WorkflowState, update: Mapping[str, Any]) -> WorkflowState:
    """Apply a reasoning response while rejecting runtime-owned fields."""

    forbidden = sorted(name for name in update if name not in MODEL_WRITABLE_FIELDS)
    if forbidden:
        raise PermissionError("model may not modify workflow fields: " + ", ".join(forbidden))
    merged = dict(state)
    for name, value in update.items():
        if name == "unknowns":
            merged[name] = [str(item) for item in value]
        elif name == "paused":
            merged[name] = bool(value)
        elif name in {"current_hypothesis", "next_goal"}:
            merged[name] = str(value) if value is not None else None
        else:
            merged[name] = _json_value(value)
    return merged  # type: ignore[return-value]


def restore_workflow_state(payload: Mapping[str, Any] | str) -> WorkflowState:
    """Restore a complete state from JSON or a JSON-safe mapping."""

    data = json.loads(payload) if isinstance(payload, str) else dict(payload)
    if not isinstance(data, dict):
        raise ValueError("workflow state payload must be an object")
    state = initial_workflow_state(
        data.get("challenge") or {},
        run_dir=str(data.get("run_dir") or ""),
        max_iterations=_positive_int(data.get("max_iterations"), default=20),
    )
    for name in STATE_FIELD_POLICIES:
        if name not in data:
            continue
        value = data[name]
        if name == "unknowns":
            state[name] = [str(item) for item in value] if isinstance(value, list) else []
        elif name in _EVIDENCE_LIST_FIELDS:
            state[name] = append_evidence_items([], value)  # type: ignore[literal-required]
        elif name == "experiments":
            state[name] = merge_experiment_items([], value)  # type: ignore[literal-required]
        elif name in _LIST_FIELDS:
            state[name] = append_items([], value)  # type: ignore[literal-required]
        elif name in {"iteration", "max_iterations"}:
            state[name] = _positive_int(value, default=0) if name == "iteration" else _positive_int(value, default=20)  # type: ignore[literal-required]
        elif name == "consecutive_replans":
            state[name] = _positive_int(value, default=0)  # type: ignore[literal-required]
        elif name in {"paused", "solved", "replan_required"}:
            state[name] = bool(value)  # type: ignore[literal-required]
        elif name in {"current_hypothesis", "next_goal", "failure_reason", "pause_reason", "pending_human_question"}:
            state[name] = str(value) if value is not None else None  # type: ignore[literal-required]
        elif name in {"run_dir", "phase"}:
            state[name] = str(value)  # type: ignore[literal-required]
        elif name == "challenge":
            state[name] = _json_value(value)  # type: ignore[literal-required]
        elif name in {"last_experiment_assessment", "selected_experiment"}:
            state[name] = _json_value(value) if isinstance(value, Mapping) else None  # type: ignore[literal-required]
    return state


_EVIDENCE_LIST_FIELDS = {"confirmed_facts", "constraints", "anomalies", "evidence_deltas"}


_LIST_FIELDS = {
    *_EVIDENCE_LIST_FIELDS,
    "hypotheses", "candidate_chains", "hypothesis_updates", "experiment_assessments", "experiments",
    "observations", "events", "artifacts", "tool_calls", "failed_actions",
    "verified_candidates", "human_decisions", "memory_matches", "skill_notes",
}


def _evidence_item(item: object) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return {str(key): _json_value(value) for key, value in item.items()}
    return {"value": _json_value(item)}


def _evidence_key(item: Mapping[str, Any]) -> str:
    material = {str(key): value for key, value in item.items() if key != "provenance"}
    return json.dumps(_json_value(material), ensure_ascii=False, sort_keys=True)


def _merge_experiment(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = {str(key): _json_value(value) for key, value in existing.items()}
    for key, value in incoming.items():
        if key == "history":
            merged["history"] = append_items(merged.get("history", []), value)
        else:
            merged[str(key)] = _json_value(value)
    return merged


def _merge_provenance(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    provenances: list[Any] = []
    for source in (existing.get("provenance"), incoming.get("provenance")):
        provenances.extend(_provenance_items(source))
    if provenances:
        unique: list[Any] = []
        seen: set[str] = set()
        for item in provenances:
            key = json.dumps(_json_value(item), ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            unique.append(_json_value(item))
        merged["provenance"] = unique[0] if len(unique) == 1 else unique
    return {str(key): _json_value(value) for key, value in merged.items()}


def _provenance_items(value: object) -> list[Any]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return [_json_value(value)]


def _positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _json_value(value: object) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return value
