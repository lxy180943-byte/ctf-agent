"""Single-responsibility LangGraph nodes for the authorized CTF workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from ctf_agent.core.models import utc_now
from ctf_agent.core.redaction import REDACTION, redact_string, redact_value
from ctf_agent.core.trace import TraceEvent, TraceStore
from ctf_agent.graph.evidence_delta import derive_evidence_delta
from ctf_agent.graph.experiment_policy import assess_experiment, fingerprint_experiment
from ctf_agent.graph.state import WorkflowState
from ctf_agent.knowledge.skill_index import SkillIndex
from ctf_agent.llm.risk import RiskLevel, classify_command_risk
from ctf_agent.memory.store import MemoryStore
from ctf_agent.pydantic_agent.models import AskVerifierInput as PlanAskVerifierInput, ExperimentPlan, HttpRequestInput as PlanHttpRequestInput, InspectBinaryInput as PlanInspectBinaryInput, PauseForHumanInput as PlanPauseForHumanInput, ReadFileInput as PlanReadFileInput, RunCommandInput as PlanRunCommandInput, SearchArtifactsInput as PlanSearchArtifactsInput, SolverDecision
from ctf_agent.sandbox.network_policy import local_executor_network_note
from ctf_agent.sandbox.executor import WorkspaceBoundaryError, resolve_inside
from ctf_agent.pydantic_agent.tools import (
    AskVerifierInput, HttpRequestInput, InspectBinaryInput, PauseForHumanInput,
    ReadFileInput, RunCommandInput, SearchArtifactsInput, ToolDependencies,
    ask_verifier, http_request, inspect_binary, pause_for_human, read_file,
    run_command, search_artifacts,
)


@dataclass
class NodeRuntime:
    """Ephemeral services; deliberately excluded from persisted WorkflowState."""

    tools: ToolDependencies | None = None
    skills: SkillIndex | None = None
    memory: MemoryStore | None = None
    reasoner: Callable[[WorkflowState], SolverDecision | Mapping[str, Any]] | None = None


_RUNTIMES: dict[str, NodeRuntime] = {}


def bind_runtime(run_dir: str | Path, runtime: NodeRuntime) -> None:
    _RUNTIMES[str(Path(run_dir).expanduser())] = runtime


def clear_runtime(run_dir: str | Path) -> None:
    _RUNTIMES.pop(str(Path(run_dir).expanduser()), None)


def ingest_challenge(state: WorkflowState) -> dict[str, Any]:
    return _guard(state, "ingest_challenge", lambda: {
        "phase": "ingested",
        "confirmed_facts": [{"kind": "challenge", "id": state["challenge"].get("id"), "category": state["challenge"].get("category")}],
    })


def collect_initial_evidence(state: WorkflowState) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        root = Path(state["run_dir"])
        work_dir = root / "work"
        files = []
        for path in sorted(work_dir.rglob("*") if work_dir.exists() else [], key=lambda item: str(item)):
            if path.is_file():
                files.append({"path": str(path.relative_to(work_dir)), "size": path.stat().st_size})
        return {"phase": "evidence-collected", "observations": [{"source": "workspace", "files": files}]}
    return _guard(state, "collect_initial_evidence", work)


def retrieve_skills(state: WorkflowState) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        runtime = _runtime(state)
        if runtime.skills is None:
            return {"phase": "skills-retrieved", "skill_notes": []}
        challenge = state["challenge"]
        query = " ".join(str(challenge.get(key, "")) for key in ("title", "description", "category"))
        return {"phase": "skills-retrieved", "skill_notes": runtime.skills.search(query, category=str(challenge.get("category") or ""), limit=6)}
    return _guard(state, "retrieve_skills", work)


def retrieve_memory(state: WorkflowState) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        runtime = _runtime(state)
        if runtime.memory is None:
            return {"phase": "memory-retrieved", "memory_matches": []}
        challenge = state["challenge"]
        items = runtime.memory.search(str(challenge.get("description") or challenge.get("title") or ""), category=str(challenge.get("category") or ""), limit=5)
        return {"phase": "memory-retrieved", "memory_matches": [item.to_dict() for item in items]}
    return _guard(state, "retrieve_memory", work)


def reason_about_challenge(state: WorkflowState) -> dict[str, Any]:
    """Produces a decision patch only. It never invokes a tool or executor."""
    def work() -> dict[str, Any]:
        runtime = _runtime(state)
        raw: SolverDecision | Mapping[str, Any]
        if runtime.reasoner is None:
            raw = {"current_hypothesis": {"name": "initial triage", "claim": "Need evidence before selecting an experiment.", "evidence_for": [], "evidence_against": [], "confidence": 0.1, "falsification_test": "Collect a local artifact or authorized response."}, "confirmed_facts": [], "unknowns": ["entry point"], "candidate_chains": [], "selected_experiment": None, "next_action": "pause", "need_human": False, "stop_reason": "No reasoning provider bound."}
        else:
            raw = runtime.reasoner(state)
        decision = raw if isinstance(raw, SolverDecision) else SolverDecision.model_validate(raw)
        proposal = decision.model_dump(mode="json")
        return {"phase": "reasoned", "unknowns": decision.unknowns, "hypotheses": [decision.current_hypothesis.model_dump(mode="json")], "current_hypothesis": decision.current_hypothesis.name, "candidate_chains": [{"steps": chain} for chain in decision.candidate_chains], "next_goal": decision.selected_experiment.goal if decision.selected_experiment else decision.stop_reason, "iteration": state["iteration"] + 1, "events": [{"kind": "reasoning-decision", "decision": proposal}]}
    return _guard(state, "reason_about_challenge", work)


def select_experiment(state: WorkflowState) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        proposal = _latest_decision(state)
        selected = proposal.get("selected_experiment") if proposal else None
        if not isinstance(selected, Mapping):
            return {"phase": "experiment-selection", "selected_experiment": None, "replan_required": False, "events": [{"kind": "no-experiment"}]}
        selected_data = dict(selected)
        assessment = assess_experiment(selected_data, workflow_state=state)
        assessment_data = assessment.model_dump(mode="json")
        fingerprint = fingerprint_experiment(selected_data).model_dump(mode="json")
        assessment_record = {**assessment_data, "fingerprint": fingerprint, "iteration": state["iteration"]}
        _trace(state, "experiment-assessed", "ok", _experiment_assessment_trace(assessment_record))
        if assessment.recommended_action == "pause":
            question = assessment.missing_question or "Experiment policy requires human input before execution."
            observation = _experiment_policy_observation(assessment_record, consecutive_replans=_int(state.get("consecutive_replans"), 0), selected=selected_data)
            return {
                "phase": "paused",
                "paused": True,
                "pause_reason": question,
                "pending_human_question": question,
                "next_goal": question,
                "selected_experiment": None,
                "last_experiment_assessment": assessment_record,
                "experiment_assessments": [assessment_record],
                "replan_required": False,
                "observations": [observation],
            }
        if assessment.recommended_action == "replan":
            consecutive = _int(state.get("consecutive_replans"), 0) + 1
            observation = _experiment_policy_observation(assessment_record, consecutive_replans=consecutive, selected=selected_data)
            if consecutive > 2:
                question = "Experiment policy rejected three consecutive proposals; provide new evidence or choose a different direction."
                observation = {**observation, "recommended_action": "pause", "requested_input": question}
                return {
                    "phase": "paused",
                    "paused": True,
                    "pause_reason": question,
                    "pending_human_question": question,
                    "next_goal": question,
                    "selected_experiment": None,
                    "last_experiment_assessment": assessment_record,
                    "experiment_assessments": [assessment_record],
                    "replan_required": False,
                    "consecutive_replans": consecutive,
                    "observations": [observation],
                }
            return {
                "phase": "replan-required",
                "selected_experiment": None,
                "last_experiment_assessment": assessment_record,
                "experiment_assessments": [assessment_record],
                "replan_required": True,
                "consecutive_replans": consecutive,
                "next_goal": "Replan experiment: " + "; ".join(assessment.reasons[:3]),
                "observations": [observation],
            }
        action_type = str(selected_data.get("action_type") or "")
        safe, reason = _safety_check(action_type, selected_data, state)
        experiment = {"id": f"experiment-{state['iteration']}", "action_type": action_type, "plan": selected_data, "safety_checked": safe, "safety_reason": reason, "assessment": assessment_record, "completed": False}
        return {
            "phase": "experiment-selected",
            "selected_experiment": selected_data,
            "last_experiment_assessment": assessment_record,
            "experiment_assessments": [assessment_record],
            "replan_required": False,
            "consecutive_replans": 0,
            "experiments": [experiment],
            "failed_actions": [] if safe else [{"experiment": experiment, "reason": reason}],
        }
    return _guard(state, "select_experiment", work)


def execute_experiment(state: WorkflowState) -> dict[str, Any]:
    """Executes only the latest explicitly safety_checked experiment."""
    def work() -> dict[str, Any]:
        if state.get("replan_required"):
            return {"phase": "replan-required"}
        if state.get("paused"):
            return {"phase": "paused"}
        experiment = _latest_experiment(state)
        if not experiment or not experiment.get("safety_checked"):
            return {"phase": "execution-blocked", "failed_actions": [{"reason": "No safety-approved experiment is available."}]}
        try:
            ExperimentPlan.model_validate(experiment["plan"])
        except Exception as exc:
            failure = {"experiment_id": experiment.get("id"), "reason": str(exc)}
            observation = {"source": "experiment-validation", "error": str(exc)}
            _trace(state, "experiment-validation-failed", "error", failure)
            return {"phase": "execution-blocked", "failed_actions": [failure], "observations": [observation], "solved": False}
        runtime = _runtime(state)
        if runtime.tools is None:
            raise RuntimeError("No ToolDependencies bound for experiment execution")
        plan = ExperimentPlan.model_validate(experiment["plan"])
        if not isinstance(plan.action_input, (PlanReadFileInput, PlanSearchArtifactsInput, PlanRunCommandInput, PlanHttpRequestInput, PlanInspectBinaryInput, PlanAskVerifierInput, PlanPauseForHumanInput)):
            return {"phase": "execution-blocked", "failed_actions": [{"experiment_id": experiment["id"], "reason": "ReadFileInput and SearchArtifactsInput dispatch are implemented."}], "observations": [{"source": "experiment-validation", "error": "unsupported action input"}], "solved": False}
        raw_action_input = plan.action_input.model_dump(mode="json")
        action_input = redact_value(raw_action_input) if isinstance(plan.action_input, PlanHttpRequestInput) else raw_action_input
        risk_decision: object = experiment.get("safety_reason")
        if isinstance(plan.action_input, PlanPauseForHumanInput):
            reason = plan.action_input.reason
            call = {"experiment_id": experiment["id"], "goal": plan.goal, "action_type": plan.action_type, "action_input": action_input, "status": "paused", "risk_decision": "human review requested", "expected_signal": plan.expected_signal, "failure_signal": plan.failure_signal, "expected_signal_matched": False, "failure_signal_matched": False, "duration_seconds": 0.0, "artifact_paths": []}
            observation = {"source": "pause_for_human", "reason": reason, "requested_input": reason, "resume_goal": plan.goal, "current_hypothesis": state.get("current_hypothesis"), "iteration": state["iteration"]}
            return {"phase": "paused", "paused": True, "pause_reason": reason, "pending_human_question": reason, "next_goal": plan.goal, "tool_calls": [call], "observations": [observation]}
        if isinstance(plan.action_input, PlanInspectBinaryInput):
            try:
                _validate_solver_relative_path(plan.action_input.path)
                binary_path = resolve_inside(plan.action_input.path, runtime.tools.context.layout.work_dir)
                if not binary_path.is_file():
                    raise ValueError("Path is not a readable binary in this challenge workspace.")
            except (OSError, ValueError, WorkspaceBoundaryError) as exc:
                reason = str(exc)
                risk_decision = {"level": "low", "reason": reason}
                call = {"experiment_id": experiment["id"], "goal": plan.goal, "action_type": plan.action_type, "action_input": action_input, "status": "blocked", "risk_decision": risk_decision, "expected_signal": plan.expected_signal, "failure_signal": plan.failure_signal, "expected_signal_matched": False, "failure_signal_matched": False, "duration_seconds": 0.0, "artifact_paths": []}
                return {"phase": "execution-blocked", "tool_calls": [call], "failed_actions": [{"experiment_id": experiment["id"], "reason": reason}], "observations": [{"source": plan.action_type, "ok": False, "error": reason}]}
            risk_decision = {"level": "low", "reason": "Workspace path guard passed."}
        if isinstance(plan.action_input, PlanHttpRequestInput):
            authorized, authorization = _authorize_http_request(plan.action_input, runtime.tools, state)
            risk_decision = authorization
            if not authorized:
                reason = str(authorization["reason"])
                call = {"experiment_id": experiment["id"], "goal": plan.goal, "action_type": plan.action_type, "action_input": action_input, "status": "blocked", "risk_decision": risk_decision, "expected_signal": plan.expected_signal, "failure_signal": plan.failure_signal, "expected_signal_matched": False, "failure_signal_matched": False, "duration_seconds": 0.0, "artifact_paths": []}
                observation = {"source": plan.action_type, "ok": False, "authorization": authorization, "error": reason}
                return {"phase": "execution-blocked", "tool_calls": [call], "failed_actions": [{"experiment_id": experiment["id"], "reason": reason}], "observations": [observation]}
        if isinstance(plan.action_input, PlanRunCommandInput):
            command_risk = classify_command_risk(plan.action_input.command, str(state["challenge"].get("connection") or ""))
            risk_decision = command_risk.to_dict()
            if command_risk.level is RiskLevel.REFUSE or command_risk.confirm_required:
                reason = command_risk.reason
                call = {"experiment_id": experiment["id"], "goal": plan.goal, "action_type": plan.action_type, "action_input": action_input, "status": "blocked", "risk_decision": risk_decision, "expected_signal": plan.expected_signal, "failure_signal": plan.failure_signal, "expected_signal_matched": False, "failure_signal_matched": False, "duration_seconds": 0.0, "artifact_paths": []}
                observation = {"source": plan.action_type, "ok": False, "risk": risk_decision, "error": reason}
                return {"phase": "execution-blocked", "tool_calls": [call], "failed_actions": [{"experiment_id": experiment["id"], "reason": reason}], "observations": [observation]}
        try:
            if isinstance(plan.action_input, PlanReadFileInput):
                result = read_file(runtime.tools, ReadFileInput(path=plan.action_input.path))
            elif isinstance(plan.action_input, PlanSearchArtifactsInput):
                result = search_artifacts(runtime.tools, SearchArtifactsInput(pattern=plan.action_input.pattern))
            elif isinstance(plan.action_input, PlanRunCommandInput):
                result = run_command(runtime.tools, RunCommandInput(command=plan.action_input.command, timeout=plan.action_input.timeout))
            elif isinstance(plan.action_input, PlanHttpRequestInput):
                result = http_request(runtime.tools, HttpRequestInput(method=plan.action_input.method, url=plan.action_input.url, params=plan.action_input.params, headers=plan.action_input.headers, body=plan.action_input.body, timeout=plan.action_input.timeout))
            elif isinstance(plan.action_input, PlanInspectBinaryInput):
                result = inspect_binary(runtime.tools, InspectBinaryInput(path=plan.action_input.path))
            elif isinstance(plan.action_input, PlanAskVerifierInput):
                result = ask_verifier(runtime.tools, AskVerifierInput())
            else:
                raise RuntimeError("Unsupported validated action input")
        except Exception as exc:
            call = {"experiment_id": experiment["id"], "goal": plan.goal, "action_type": plan.action_type, "action_input": action_input, "status": "failed", "risk_decision": risk_decision, "expected_signal": plan.expected_signal, "failure_signal": plan.failure_signal, "expected_signal_matched": False, "failure_signal_matched": False, "duration_seconds": 0.0, "artifact_paths": []}
            return {"phase": "executed", "tool_calls": [call], "failed_actions": [{"experiment_id": experiment["id"], "reason": str(exc)}], "observations": [{"source": plan.action_type, "error": str(exc)}]}
        evidence_text = json.dumps(result.observation, ensure_ascii=False, sort_keys=True)
        status = "failed" if isinstance(plan.action_input, (PlanHttpRequestInput, PlanInspectBinaryInput)) and not result.ok else "executed"
        call = {"experiment_id": experiment["id"], "goal": plan.goal, "action_type": plan.action_type, "action_input": action_input, "status": status, "risk_decision": risk_decision, "expected_signal": plan.expected_signal, "failure_signal": plan.failure_signal, "expected_signal_matched": plan.expected_signal in evidence_text, "failure_signal_matched": plan.failure_signal in evidence_text, "duration_seconds": result.duration_seconds, "artifact_paths": [artifact["path"] for artifact in result.artifacts]}
        return {"phase": "executed", "tool_calls": [call], "artifacts": result.artifacts, "observations": [{"source": plan.action_type, "evidence": result.observation, "ok": result.ok}], "failed_actions": [] if result.ok else [{"experiment_id": experiment["id"], "reason": result.error or f"{plan.action_type} failed"}]}
    return _guard(state, "execute_experiment", work)


def summarize_observation(state: WorkflowState) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        call = state["tool_calls"][-1] if state["tool_calls"] else None
        observation = state["observations"][-1] if state["observations"] else None
        experiment = state["experiments"][-1] if state["experiments"] else None
        if not isinstance(call, Mapping) or not isinstance(observation, Mapping) or not isinstance(experiment, Mapping):
            return {"phase": "summarized"}
        delta = derive_evidence_delta(experiment, observation, call)
        delta_data = delta.model_dump(mode="json")
        _trace(state, "evidence-delta", "ok", _evidence_delta_trace(delta_data))
        return {
            "phase": "summarized",
            "confirmed_facts": delta_data["confirmed_facts"],
            "constraints": delta_data["constraints"],
            "anomalies": delta_data["anomalies"],
            "evidence_deltas": [delta_data],
        }
    return _guard(state, "summarize_observation", work)


def update_hypotheses(state: WorkflowState) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        delta = state["evidence_deltas"][-1] if state.get("evidence_deltas") else None
        experiment = _latest_experiment(state) or {}
        call = state["tool_calls"][-1] if state.get("tool_calls") else {}
        if not isinstance(delta, Mapping):
            return {"phase": "hypotheses-updated"}
        signal = _reconciliation_signal(delta, experiment, call)
        hypotheses, hypothesis_changes = _reconcile_items(
            state.get("hypotheses", []),
            current_name=str(state.get("current_hypothesis") or ""),
            signal=signal,
            iteration=int(state.get("iteration") or 0),
            item_kind="hypothesis",
        )
        candidate_chains, chain_changes = _reconcile_items(
            state.get("candidate_chains", []),
            current_name=str(state.get("current_hypothesis") or ""),
            signal=signal,
            iteration=int(state.get("iteration") or 0),
            item_kind="candidate_chain",
        )
        current = _select_current_hypothesis(hypotheses, state.get("current_hypothesis"))
        audit = {
            "kind": "hypothesis_reconciliation",
            "iteration": int(state.get("iteration") or 0),
            "experiment_id": experiment.get("id") if isinstance(experiment, Mapping) else None,
            "tool_call_id": call.get("id") or call.get("tool_call_id") or call.get("experiment_id") if isinstance(call, Mapping) else None,
            "delta_provenance": delta.get("provenance", {}),
            "signal": _public_signal(signal),
            "changes": hypothesis_changes + chain_changes,
        }
        _trace(state, "hypothesis-reconciled", "ok", _hypothesis_trace(audit))
        return {
            "phase": "hypotheses-updated",
            "hypotheses": hypotheses,
            "candidate_chains": candidate_chains,
            "current_hypothesis": current,
            "hypothesis_updates": [audit],
        }
    return _guard(state, "update_hypotheses", work)


def _reconciliation_signal(delta: Mapping[str, Any], experiment: Mapping[str, Any], call: Mapping[str, Any]) -> dict[str, Any]:
    confirmed = list(delta.get("confirmed_facts", []) or [])
    constraints = list(delta.get("constraints", []) or [])
    anomalies = list(delta.get("anomalies", []) or [])
    text = json.dumps({"delta": delta, "experiment": experiment, "call": call}, ensure_ascii=False, sort_keys=True).lower()
    anomaly_kinds = {str(item.get("kind")) for item in anomalies if isinstance(item, Mapping)}
    expected_hit = bool(call.get("expected_signal_matched")) and bool(confirmed)
    failure_hit = bool(call.get("failure_signal_matched")) or "failure_signal" in anomaly_kinds or "failure_signal_matched" in anomaly_kinds
    timeout = "timeout" in anomaly_kinds or bool(call.get("timed_out"))
    nonzero = "nonzero_exit" in anomaly_kinds
    risk_block = any(kind in anomaly_kinds for kind in ("authorization_or_risk_block", "tool_failure")) or str(call.get("status") or "").lower() == "blocked"
    tool_unavailable = any(word in text for word in ("tool unavailable", "no tooldependencies", "command not found", "not installed"))
    network_block = any(word in text for word in ("network authorization", "not enabled", "outside the authorized", "challenge connection is required"))
    failure_kind = _failure_kind(failure_hit, timeout, nonzero, risk_block, tool_unavailable, network_block)
    negative = failure_hit or timeout or nonzero or risk_block
    return {
        "expected_hit": expected_hit,
        "negative": negative,
        "failure_hit": failure_hit,
        "timeout": timeout,
        "nonzero": nonzero,
        "risk_block": risk_block,
        "tool_unavailable": tool_unavailable,
        "network_block": network_block,
        "external_block_only": (tool_unavailable or network_block) and not failure_hit and not nonzero,
        "failure_kind": failure_kind,
        "confirmed_summaries": _evidence_summaries(confirmed),
        "anomaly_summaries": _evidence_summaries(anomalies),
        "constraint_summaries": _evidence_summaries(constraints),
        "context_text": text[:4000],
    }


def _reconcile_items(items: Any, *, current_name: str, signal: Mapping[str, Any], iteration: int, item_kind: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    values = list(items or [])
    normalized = [_normalize_reconcilable(item, index, item_kind) for index, item in enumerate(values)]
    target_index = _target_index(normalized, current_name)
    changes: list[dict[str, Any]] = []
    if target_index is None:
        return normalized, changes
    item = dict(normalized[target_index])
    before = {"confidence": item.get("confidence"), "status": item.get("status"), "failed_experiment_count": item.get("failed_experiment_count", 0)}
    _apply_signal(item, signal, iteration)
    normalized[target_index] = item
    changes.append({
        "item_kind": item_kind,
        "id": item.get("id"),
        "name": item.get("name"),
        "before": before,
        "after": {"confidence": item.get("confidence"), "status": item.get("status"), "failed_experiment_count": item.get("failed_experiment_count", 0)},
        "update_reason": item.get("update_reason"),
    })
    return normalized, changes


def _normalize_reconcilable(item: Any, index: int, item_kind: str) -> dict[str, Any]:
    data = dict(item) if isinstance(item, Mapping) else {"value": item}
    if item_kind == "candidate_chain":
        steps = data.get("steps") if isinstance(data.get("steps"), list) else data.get("value")
        name = str(data.get("name") or data.get("id") or (steps[0] if isinstance(steps, list) and steps else f"chain-{index}"))
        claim = str(data.get("claim") or " -> ".join(str(step) for step in steps) if isinstance(steps, list) else data.get("claim") or name)
    else:
        name = str(data.get("name") or data.get("id") or f"hypothesis-{index}")
        claim = str(data.get("claim") or data.get("hypothesis") or data.get("summary") or name)
    data["id"] = str(data.get("id") or name)
    data["name"] = name
    data["claim"] = claim
    data["confidence"] = _clamp(data.get("confidence", 0.5))
    data["status"] = str(data.get("status") or "active")
    data["evidence_for"] = [str(item) for item in data.get("evidence_for", [])] if isinstance(data.get("evidence_for", []), list) else [str(data.get("evidence_for"))]
    data["evidence_against"] = [str(item) for item in data.get("evidence_against", [])] if isinstance(data.get("evidence_against", []), list) else [str(data.get("evidence_against"))]
    data["update_reason"] = str(data.get("update_reason") or "not yet reconciled")
    data["failed_experiment_count"] = _int(data.get("failed_experiment_count"), 0)
    data["last_updated_iteration"] = _int(data.get("last_updated_iteration"), 0)
    if data.get("last_failure_kind") is not None:
        data["last_failure_kind"] = str(data.get("last_failure_kind"))
    return data


def _apply_signal(item: dict[str, Any], signal: Mapping[str, Any], iteration: int) -> None:
    confidence = _clamp(item.get("confidence", 0.5))
    status = str(item.get("status") or "active")
    evidence_for = list(item.get("evidence_for", []))
    evidence_against = list(item.get("evidence_against", []))
    reason = "no new decisive evidence"
    failed_count = _int(item.get("failed_experiment_count"), 0)
    failure_kind = str(signal.get("failure_kind") or "none")
    falsified = _falsification_hit(item, signal)
    if signal.get("expected_hit"):
        confidence = _clamp(confidence + 0.15)
        status = "active" if status != "superseded" else status
        failed_count = 0
        evidence_for.extend(signal.get("confirmed_summaries", [])[:3])
        reason = "expected signal matched confirmed facts"
        item.pop("last_failure_kind", None)
    if signal.get("negative"):
        confidence = _clamp(confidence - 0.15)
        same_failure = item.get("last_failure_kind") == failure_kind
        failed_count = failed_count + 1 if same_failure else 1
        item["last_failure_kind"] = failure_kind
        evidence_against.extend((signal.get("anomaly_summaries", []) or signal.get("constraint_summaries", []))[:4])
        reason = f"negative evidence observed: {failure_kind}"
        if status == "active":
            status = "weakened"
    if signal.get("negative") and falsified and not signal.get("external_block_only"):
        confidence = min(confidence, 0.10)
        status = "falsified"
        reason = "falsification test matched observed evidence"
    elif failed_count >= 2 and not signal.get("external_block_only"):
        confidence = min(confidence, 0.10)
        status = "falsified" if signal.get("failure_hit") else "weakened"
        reason = f"two consecutive {failure_kind} failures"
    elif signal.get("external_block_only") and signal.get("negative"):
        status = "weakened" if status != "superseded" else status
        reason = f"execution blocked externally: {failure_kind}"
    item["confidence"] = _clamp(confidence)
    item["status"] = status
    item["evidence_for"] = _dedupe_strings(evidence_for)[:30]
    item["evidence_against"] = _dedupe_strings(evidence_against)[:30]
    item["update_reason"] = reason
    item["failed_experiment_count"] = failed_count
    item["last_updated_iteration"] = iteration


def _select_current_hypothesis(hypotheses: list[dict[str, Any]], previous: Any) -> str | None:
    previous_name = str(previous or "")
    for item in hypotheses:
        if item.get("name") == previous_name and item.get("status") != "falsified":
            return str(item.get("name"))
    active = [item for item in hypotheses if item.get("status") == "active"]
    if not active:
        active = [item for item in hypotheses if item.get("status") == "weakened"]
    if not active:
        return None
    return str(max(active, key=lambda item: float(item.get("confidence", 0.0))).get("name"))


def _target_index(items: list[dict[str, Any]], current_name: str) -> int | None:
    if not items:
        return None
    for index, item in enumerate(items):
        if current_name and current_name in {str(item.get("name")), str(item.get("id"))}:
            return index
    return 0


def _falsification_hit(item: Mapping[str, Any], signal: Mapping[str, Any]) -> bool:
    test = str(item.get("falsification_test") or "").strip().lower()
    if not test:
        return False
    context = str(signal.get("context_text") or "").lower()
    failure_kind = str(signal.get("failure_kind") or "").lower()
    if test in context:
        return True
    return bool(signal.get("failure_hit") and failure_kind and failure_kind in test)


def _failure_kind(failure_hit: bool, timeout: bool, nonzero: bool, risk_block: bool, tool_unavailable: bool, network_block: bool) -> str:
    if tool_unavailable:
        return "tool_unavailable"
    if network_block:
        return "network_block"
    if failure_hit:
        return "failure_signal"
    if timeout:
        return "timeout"
    if nonzero:
        return "nonzero_exit"
    if risk_block:
        return "risk_block"
    return "none"


def _evidence_summaries(items: list[Any]) -> list[str]:
    summaries: list[str] = []
    for item in items:
        if isinstance(item, Mapping):
            summaries.append(_sanitize_reconciliation_text(item.get("summary") or item.get("kind") or item))
        else:
            summaries.append(_sanitize_reconciliation_text(item))
    return _dedupe_strings(summaries)


def _sanitize_reconciliation_text(value: Any) -> str:
    text = redact_string(str(redact_value(value)))
    text = text.replace("Authorization", REDACTION).replace("authorization", REDACTION)
    text = text.replace("Bearer", REDACTION).replace("bearer", REDACTION)
    text = text.replace("api_key", REDACTION).replace("API_KEY", REDACTION)
    return text[:500]


def _hypothesis_trace(audit: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "experiment_id": audit.get("experiment_id"),
        "tool_call_id": audit.get("tool_call_id"),
        "delta_provenance": audit.get("delta_provenance"),
        "signal": {key: audit.get("signal", {}).get(key) for key in ("expected_hit", "negative", "failure_kind", "external_block_only")},
        "changes": audit.get("changes", []),
    }


def _public_signal(signal: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "expected_hit": bool(signal.get("expected_hit")),
        "negative": bool(signal.get("negative")),
        "failure_hit": bool(signal.get("failure_hit")),
        "timeout": bool(signal.get("timeout")),
        "nonzero": bool(signal.get("nonzero")),
        "risk_block": bool(signal.get("risk_block")),
        "tool_unavailable": bool(signal.get("tool_unavailable")),
        "network_block": bool(signal.get("network_block")),
        "external_block_only": bool(signal.get("external_block_only")),
        "failure_kind": str(signal.get("failure_kind") or "none"),
        "confirmed_summaries": list(signal.get("confirmed_summaries", []) or [])[:3],
        "anomaly_summaries": list(signal.get("anomaly_summaries", []) or [])[:4],
        "constraint_summaries": list(signal.get("constraint_summaries", []) or [])[:4],
    }


def _clamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.5
    return max(0.0, min(1.0, round(number, 4)))


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dedupe_strings(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _experiment_assessment_trace(assessment: Mapping[str, Any]) -> dict[str, Any]:
    fingerprint = assessment.get("fingerprint", {}) if isinstance(assessment.get("fingerprint"), Mapping) else {}
    return {
        "action_type": fingerprint.get("action_type"),
        "fingerprint_digest": fingerprint.get("digest"),
        "recommended_action": assessment.get("recommended_action"),
        "allowed": bool(assessment.get("allowed")),
        "duplicate": bool(assessment.get("duplicate")),
        "blocked_by_constraint": bool(assessment.get("blocked_by_constraint")),
        "information_gain_score": assessment.get("information_gain_score"),
        "risk_penalty": assessment.get("risk_penalty"),
        "prior_attempt_count": len(assessment.get("prior_attempt_ids", []) or []),
        "reasons": [_sanitize_reconciliation_text(reason) for reason in list(assessment.get("reasons", []) or [])[:5]],
        "missing_question": _sanitize_reconciliation_text(assessment.get("missing_question")) if assessment.get("missing_question") else None,
    }


def _experiment_policy_observation(assessment: Mapping[str, Any], *, consecutive_replans: int, selected: Mapping[str, Any]) -> dict[str, Any]:
    trace = _experiment_assessment_trace(assessment)
    return {
        "source": "experiment_policy",
        "kind": "experiment_assessment",
        "status": "blocked" if assessment.get("blocked_by_constraint") else "replan_required",
        "recommended_action": assessment.get("recommended_action"),
        "goal": _sanitize_reconciliation_text(selected.get("goal") or ""),
        "action_type": trace.get("action_type"),
        "fingerprint_digest": trace.get("fingerprint_digest"),
        "duplicate": trace.get("duplicate"),
        "blocked_by_constraint": trace.get("blocked_by_constraint"),
        "information_gain_score": trace.get("information_gain_score"),
        "risk_penalty": trace.get("risk_penalty"),
        "reasons": trace.get("reasons", []),
        "missing_question": trace.get("missing_question"),
        "prior_attempt_count": trace.get("prior_attempt_count"),
        "consecutive_replans": consecutive_replans,
    }


def verify_candidates(state: WorkflowState) -> dict[str, Any]:
    """The sole node allowed to update verified_candidates and solved."""
    def work() -> dict[str, Any]:
        runtime = _runtime(state)
        if runtime.tools is None:
            return {"phase": "verified", "verified_candidates": [], "solved": False}
        before = len(runtime.tools.context.state.flag_candidates)
        result = ask_verifier(runtime.tools, AskVerifierInput())
        candidates = runtime.tools.context.state.flag_candidates[before:]
        verified = [candidate.to_dict() for candidate in candidates if candidate.verified]
        return {"phase": "verified", "verified_candidates": verified, "solved": bool(verified), "observations": [{"source": "verifier", "evidence": result.observation}]}
    return _guard(state, "verify_candidates", work)


def human_review(state: WorkflowState) -> dict[str, Any]:
    return _guard(state, "human_review", lambda: {"phase": "human-review", "paused": True})


def finish_run(state: WorkflowState) -> dict[str, Any]:
    return _guard(state, "finish_run", lambda: {"phase": "finished"})


def fail_run(state: WorkflowState) -> dict[str, Any]:
    return _guard(state, "fail_run", lambda: {"phase": "failed", "failure_reason": state.get("failure_reason") or "Graph workflow failed."})


def _evidence_delta_trace(delta: Mapping[str, Any]) -> dict[str, Any]:
    provenance = delta.get("provenance", {}) if isinstance(delta.get("provenance"), Mapping) else {}
    return {
        "source_type": provenance.get("source_type"),
        "source_id": provenance.get("source_id"),
        "tool_call_id": provenance.get("tool_call_id"),
        "confirmed_fact_count": len(delta.get("confirmed_facts", []) or []),
        "constraint_count": len(delta.get("constraints", []) or []),
        "anomaly_count": len(delta.get("anomalies", []) or []),
        "candidate_artifact_count": len(delta.get("candidate_artifacts", []) or []),
        "notes": list(delta.get("extraction_notes", []) or [])[:5],
    }


def _guard(state: WorkflowState, node: str, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        update = operation()
        _trace(state, node, "ok", update)
        return {**update, "events": [*update.get("events", []), {"node": node, "status": "ok", "at": utc_now()}]}
    except Exception as exc:
        error = {"node": node, "status": "error", "error": str(exc), "at": utc_now()}
        _trace(state, node, "error", error)
        return {"phase": "error", "failure_reason": f"{node}: {exc}", "events": [error]}


def _trace(state: WorkflowState, node: str, status: str, data: Mapping[str, Any]) -> None:
    TraceStore(Path(state["run_dir"]) / "trace.jsonl").append(TraceEvent(challenge_id=str(state["challenge"].get("id", "unknown")), agent="langgraph", action=node, stdout=json.dumps(dict(data), ensure_ascii=False, sort_keys=True), metadata={"status": status}))


def _runtime(state: WorkflowState) -> NodeRuntime:
    return _RUNTIMES.get(str(Path(state["run_dir"]).expanduser()), NodeRuntime())


def _latest_decision(state: WorkflowState) -> Mapping[str, Any]:
    for event in reversed(state["events"]):
        if event.get("kind") == "reasoning-decision":
            return event.get("decision", {})
    return {}


def _latest_experiment(state: WorkflowState) -> Mapping[str, Any] | None:
    return state["experiments"][-1] if state["experiments"] else None


def _safety_check(action: str, plan: Mapping[str, Any], state: WorkflowState) -> tuple[bool, str]:
    if action not in {"read_file", "search_artifacts", "run_command", "http_request", "inspect_binary", "ask_verifier", "pause"}:
        return False, "Unsupported structured action."
    if action == "run_command":
        risk = classify_command_risk(str(plan.get("command") or ""), str(state["challenge"].get("connection") or ""))
        if risk.level is RiskLevel.REFUSE or risk.confirm_required:
            return False, risk.reason
    return True, "Passed graph safety gate; tool will revalidate at execution."


def _validate_solver_relative_path(path: str) -> None:
    raw = str(path).strip()
    candidate = Path(raw)
    if not raw or candidate.is_absolute() or PureWindowsPath(raw).is_absolute():
        raise ValueError(f"Path is not a challenge-relative file path: {path}")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"Path is outside workspace: unsafe segment in {path}")


def _execute_tool(deps: ToolDependencies, action: str, plan: Mapping[str, Any]):
    action_input = dict(plan.get("action_input") or {})
    action_input.pop("type", None)
    if action == "read_file": return read_file(deps, ReadFileInput(**action_input))
    if action == "search_artifacts": return search_artifacts(deps, SearchArtifactsInput(**action_input))
    if action == "run_command": return run_command(deps, RunCommandInput(**action_input))
    if action == "http_request": return http_request(deps, HttpRequestInput(**action_input))
    if action == "inspect_binary": return inspect_binary(deps, InspectBinaryInput(**action_input))
    if action == "ask_verifier": return ask_verifier(deps, AskVerifierInput())
    return pause_for_human(deps, PauseForHumanInput(**action_input))


def _authorize_http_request(request: PlanHttpRequestInput, deps: ToolDependencies, state: WorkflowState) -> tuple[bool, dict[str, Any]]:
    """Validate one request against the explicit authorized challenge connection."""
    policy = local_executor_network_note(deps.context.config, deps.context.state.challenge)
    decision = {**policy.to_dict(), "url": redact_value(request.url), "redirects_followed": False}
    connection = (deps.context.state.challenge.connection or "").strip()
    if not connection:
        decision["reason"] = "HTTP request denied: challenge connection is required."
        return False, decision
    if not policy.allowed:
        return False, decision
    try:
        target = urlparse(request.url)
        expected = urlparse(connection if "://" in connection else f"//{connection}")
        if target.scheme not in {"http", "https"} or not target.hostname:
            decision["reason"] = "HTTP request denied: only absolute http(s) URLs are supported."
            return False, decision
        if target.hostname.lower() != (expected.hostname or "").lower() or _http_port(target) != _http_port(expected):
            decision["reason"] = "HTTP request denied: URL is outside the authorized challenge connection."
            return False, decision
    except ValueError:
        decision["reason"] = "HTTP request denied: URL or connection port is invalid."
        return False, decision
    limit = _max_network_requests(deps.context.config)
    used = sum(call.get("action_type") == "http_request" for call in state.get("tool_calls", []) if isinstance(call, Mapping))
    if used >= limit:
        decision["reason"] = f"HTTP request denied: network request limit ({limit}) reached."
        return False, decision
    decision["reason"] = "Authorized challenge connection; redirects are not followed."
    return True, decision


def _http_port(parsed: Any) -> int | None:
    return parsed.port if parsed.port is not None else {"http": 80, "https": 443}.get(parsed.scheme)


def _max_network_requests(config: Mapping[str, Any]) -> int:
    try:
        return max(1, int(config.get("graph", {}).get("max_network_requests", 12)))
    except (AttributeError, TypeError, ValueError):
        return 12
prepare_context_node = ingest_challenge
reason_node = reason_about_challenge
execute_node = execute_experiment
summarize_node = summarize_observation
verify_node = verify_candidates
finalize_node = finish_run
