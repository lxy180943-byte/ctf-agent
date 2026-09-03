"""Pure experiment de-duplication and information-gain policy."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, Literal, Mapping
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from ctf_agent.core.redaction import REDACTION, is_sensitive_key, redact_string, redact_value


Recommendation = Literal["proceed", "replan", "pause"]

_TEXT_LIMIT = 180


class ExperimentFingerprint(BaseModel):
    """Stable, redacted identity for a proposed experiment."""

    model_config = ConfigDict(extra="forbid")

    action_type: str
    normalized_input: dict[str, Any] = Field(default_factory=dict)
    digest: str


class ExperimentAssessment(BaseModel):
    """Deterministic policy decision for a proposed experiment."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    duplicate: bool
    blocked_by_constraint: bool
    information_gain_score: float = Field(ge=0.0, le=1.0)
    risk_penalty: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    missing_question: str | None = None
    prior_attempt_ids: list[str] = Field(default_factory=list)
    recommended_action: Recommendation


def fingerprint_experiment(experiment: Any) -> ExperimentFingerprint:
    """Return a comparable fingerprint without retaining secret values."""

    data = _experiment_data(experiment)
    action_type = str(data.get("action_type") or _mapping(data.get("action_input")).get("type") or "unknown")
    action_input = _mapping(data.get("action_input"))
    normalized = _normalize_action_input(action_type, action_input)
    material = {"action_type": action_type, "normalized_input": normalized}
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    return ExperimentFingerprint(action_type=action_type, normalized_input=normalized, digest=digest)


def assess_experiment(experiment: Any, *, workflow_state: Mapping[str, Any]) -> ExperimentAssessment:
    """Assess a proposed experiment without mutating state or invoking tools."""

    data = _experiment_data(experiment)
    fp = fingerprint_experiment(data)
    matching = [attempt for attempt in _prior_attempts(workflow_state) if attempt["fingerprint"].digest == fp.digest]
    prior_ids = _dedupe([str(attempt["id"]) for attempt in matching if attempt.get("id")])
    reasons: list[str] = []

    duplicate = False
    if matching:
        last = matching[-1]
        if _inconclusive_failure(last):
            reasons.append("prior matching attempt was inconclusive: timeout or tool unavailable")
        elif _has_new_relevant_evidence(workflow_state, fp, prior_ids):
            reasons.append("new relevant confirmed facts or constraints allow a focused retry")
        else:
            duplicate = True
            reasons.append("matching experiment already ran without new relevant evidence")

    consecutive_failure = _two_consecutive_failures(matching)
    if consecutive_failure:
        duplicate = True
        reasons.append("same experiment failed twice consecutively")

    blocked, block_reasons, missing_question = _blocked_by_constraints(data, fp, workflow_state)
    reasons.extend(block_reasons)
    risk_penalty = _risk_penalty(data, fp)
    information_gain = _information_gain(data, fp, workflow_state, duplicate=duplicate, blocked=blocked, risk_penalty=risk_penalty)
    allowed = not blocked and not duplicate
    if blocked and missing_question:
        recommendation: Recommendation = "pause"
    elif blocked or duplicate or consecutive_failure:
        recommendation = "replan"
    else:
        recommendation = "proceed"
    return ExperimentAssessment(
        allowed=allowed,
        duplicate=duplicate,
        blocked_by_constraint=blocked,
        information_gain_score=_clamp(information_gain),
        risk_penalty=_clamp(risk_penalty),
        reasons=_dedupe(reasons),
        missing_question=missing_question,
        prior_attempt_ids=prior_ids,
        recommended_action=recommendation,
    )


def _normalize_action_input(action_type: str, action_input: Mapping[str, Any]) -> dict[str, Any]:
    if action_type in {"read_file", "inspect_binary"}:
        return {"path": _normalize_path(action_input.get("path"))}
    if action_type == "search_artifacts":
        pattern = _safe_text(action_input.get("pattern"))
        return {"pattern_hash": _hash_text(pattern), "pattern_length": len(pattern)}
    if action_type == "http_request":
        return _normalize_http(action_input)
    if action_type == "run_command":
        command = redact_string(str(action_input.get("command") or ""))
        return {"command_summary": _safe_text(command), "command_hash": _hash_text(command), "timeout": _safe_int(action_input.get("timeout"))}
    if action_type == "ask_verifier":
        return {}
    if action_type == "pause":
        reason = _safe_text(action_input.get("reason"))
        return {"reason_hash": _hash_text(reason), "reason_length": len(reason)}
    return _json_safe(redact_value(action_input))


def _normalize_http(action_input: Mapping[str, Any]) -> dict[str, Any]:
    raw_url = str(action_input.get("url") or "")
    parsed = urlsplit(raw_url)
    param_names = {name for name, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    params = _mapping(action_input.get("params"))
    param_names.update(str(name) for name in params)
    headers = _mapping(action_input.get("headers"))
    body = action_input.get("body")
    return {
        "method": str(action_input.get("method") or "GET").upper(),
        "scheme": parsed.scheme.lower(),
        "host": (parsed.hostname or "").lower(),
        "port": parsed.port,
        "path": _normalize_url_path(parsed.path),
        "param_names": sorted(param_names),
        "header_names": sorted(str(name).lower() for name in headers),
        "body_present": body not in (None, ""),
        "body_hash": _hash_text(redact_string(str(body))) if body not in (None, "") else None,
        "timeout": _safe_int(action_input.get("timeout")),
    }


def _prior_attempts(workflow_state: Mapping[str, Any]) -> list[dict[str, Any]]:
    experiments = [_mapping(item) for item in _as_list(workflow_state.get("experiments"))]
    experiments_by_id = {str(item.get("id")): item for item in experiments if item.get("id")}
    attempts: list[dict[str, Any]] = []
    for experiment in experiments:
        try:
            attempts.append({
                "id": str(experiment.get("id") or f"experiment-{len(attempts)}"),
                "fingerprint": fingerprint_experiment(experiment),
                "status": str(experiment.get("status") or ""),
                "experiment": experiment,
            })
        except Exception:
            continue
    for call in _as_list(workflow_state.get("tool_calls")):
        tool_call = _mapping(call)
        experiment_id = str(tool_call.get("experiment_id") or tool_call.get("id") or f"tool-call-{len(attempts)}")
        merged = {**experiments_by_id.get(experiment_id, {}), **tool_call}
        if "action_input" not in merged and isinstance(experiments_by_id.get(experiment_id, {}).get("plan"), Mapping):
            merged["action_input"] = experiments_by_id[experiment_id]["plan"].get("action_input")
        try:
            attempts.append({
                "id": experiment_id,
                "fingerprint": fingerprint_experiment(merged),
                "status": str(tool_call.get("status") or ""),
                "tool_call": tool_call,
                "experiment": experiments_by_id.get(experiment_id, {}),
            })
        except Exception:
            continue
    return attempts


def _blocked_by_constraints(data: Mapping[str, Any], fp: ExperimentFingerprint, workflow_state: Mapping[str, Any]) -> tuple[bool, list[str], str | None]:
    reasons: list[str] = []
    missing_question: str | None = None
    text = _safe_json(data).lower()
    action_type = fp.action_type
    action_input = _mapping(data.get("action_input"))
    combined = _evidence_items(workflow_state, "constraints") + _evidence_items(workflow_state, "anomalies")

    if action_type == "http_request":
        blocked, reason = _network_blocked(action_input, combined)
        if blocked:
            reasons.append(reason)
            missing_question = "Confirm an authorized challenge network scope before making this request."
    if action_type in {"read_file", "inspect_binary"}:
        blocked, reason = _path_blocked(str(fp.normalized_input.get("path") or ""), combined)
        if blocked:
            reasons.append(reason)
    for item in combined:
        kind = str(item.get("kind") or "").lower()
        summary = _safe_json(item).lower()
        if kind in {"php_blacklist", "blacklist", "path_blacklist"}:
            pattern = str(_mapping(item.get("data")).get("pattern") or item.get("summary") or "").lower()
            if pattern and pattern in text:
                reasons.append("experiment input conflicts with a known blacklist constraint")
        if kind in {"risk_refused", "authorization_or_risk_block"} and action_type in {"run_command", "http_request"}:
            reasons.append("experiment conflicts with a prior risk or authorization block")
        if kind in {"tool_blocked", "tool_limit"} and "limit" in summary and action_type in summary:
            reasons.append("experiment conflicts with a known tool limit")
    if str(data.get("risk") or "").lower() == "high" and _has_risk_refusal(combined):
        reasons.append("high-risk experiment conflicts with observed risk refusal")
    return bool(reasons), _dedupe(reasons), missing_question


def _network_blocked(action_input: Mapping[str, Any], evidence: list[dict[str, Any]]) -> tuple[bool, str]:
    parsed = urlsplit(str(action_input.get("url") or ""))
    target_host = (parsed.hostname or "").lower()
    if not target_host:
        return True, "HTTP experiment has no absolute authorized host"
    allowed_hosts: set[str] = set()
    explicit_denial = False
    for item in evidence:
        data = _mapping(item.get("data"))
        value = data.get("value") if isinstance(data.get("value"), Mapping) else data
        value_map = _mapping(value)
        if value_map.get("allowed") is False:
            explicit_denial = True
        for key in ("host", "hostname", "allowed_host"):
            if value_map.get(key):
                allowed_hosts.add(str(value_map[key]).lower())
        for host in _as_list(value_map.get("allowed_hosts")):
            allowed_hosts.add(str(host).lower())
        connection = value_map.get("connection") or data.get("connection")
        if connection:
            host = urlsplit(str(connection) if "://" in str(connection) else f"//{connection}").hostname
            if host:
                allowed_hosts.add(host.lower())
    if explicit_denial:
        return True, "network authorization constraint denies HTTP requests"
    if allowed_hosts and target_host not in allowed_hosts:
        return True, "HTTP experiment is outside the known authorized network scope"
    return False, ""


def _path_blocked(path: str, evidence: list[dict[str, Any]]) -> tuple[bool, str]:
    for item in evidence:
        kind = str(item.get("kind") or "").lower()
        data = _mapping(item.get("data"))
        value = _mapping(data.get("value")) if isinstance(data.get("value"), Mapping) else data
        denied = {str(entry) for entry in _as_list(value.get("denied_paths") or value.get("blocked_paths"))}
        blacklist = {str(entry) for entry in _as_list(value.get("blacklist") or value.get("blacklist_patterns"))}
        if kind in {"path_blacklist", "tool_blocked"} and (value.get("path") or value.get("reason")):
            blacklist.add(str(value.get("path") or value.get("reason")))
        if any(entry and entry in path for entry in denied | blacklist):
            return True, "path experiment conflicts with a known path constraint"
    return False, ""


def _information_gain(data: Mapping[str, Any], fp: ExperimentFingerprint, workflow_state: Mapping[str, Any], *, duplicate: bool, blocked: bool, risk_penalty: float) -> float:
    if blocked:
        return 0.0
    score = 0.45
    if fp.action_type in {"read_file", "inspect_binary"}:
        score = 0.85 if not _path_seen(str(fp.normalized_input.get("path") or ""), workflow_state) else 0.25
    elif fp.action_type == "search_artifacts":
        score = 0.65
    elif fp.action_type in {"http_request", "run_command"}:
        score = 0.55
    elif fp.action_type == "ask_verifier":
        score = 0.35
    elif fp.action_type == "pause":
        score = 0.20
    if _tests_active_hypothesis(data, workflow_state):
        score += 0.15
    if _addresses_unknown(data, workflow_state):
        score += 0.10
    if duplicate:
        score -= 0.40
    score -= risk_penalty * 0.25
    return score


def _risk_penalty(data: Mapping[str, Any], fp: ExperimentFingerprint) -> float:
    risk = str(data.get("risk") or "").lower()
    penalty = {"low": 0.05, "medium": 0.25, "high": 0.65}.get(risk, 0.15)
    if fp.action_type == "run_command":
        penalty += 0.10
    elif fp.action_type == "http_request":
        penalty += 0.05
    return _clamp(penalty)


def _two_consecutive_failures(attempts: list[dict[str, Any]]) -> bool:
    failures = [_failure_kind(attempt) for attempt in attempts if _failure_kind(attempt) != "ok"]
    return len(failures) >= 2 and failures[-1] == failures[-2]


def _inconclusive_failure(attempt: Mapping[str, Any]) -> bool:
    return _failure_kind(attempt) in {"timeout", "tool_unavailable"}


def _failure_kind(attempt: Mapping[str, Any]) -> str:
    call = _mapping(attempt.get("tool_call"))
    status = str(call.get("status") or attempt.get("status") or "").lower()
    text = _safe_json(attempt).lower()
    if call.get("failure_signal_matched"):
        return "failure_signal"
    if call.get("timed_out") or "timeout" in text:
        return "timeout"
    if "tool unavailable" in text or "command not found" in text or "not installed" in text:
        return "tool_unavailable"
    if status in {"failed", "blocked"}:
        return status
    return "ok"


def _has_new_relevant_evidence(workflow_state: Mapping[str, Any], fp: ExperimentFingerprint, prior_ids: list[str]) -> bool:
    prior = set(prior_ids)
    material = _fingerprint_terms(fp)
    for item in _evidence_items(workflow_state, "confirmed_facts") + _evidence_items(workflow_state, "constraints"):
        provenance = item.get("provenance")
        provenance_items = provenance if isinstance(provenance, list) else [provenance]
        source_ids = {str(_mapping(prov).get("source_id") or _mapping(prov).get("tool_call_id") or "") for prov in provenance_items}
        if source_ids and source_ids <= prior:
            continue
        text = _safe_json(item).lower()
        if not material or any(term in text for term in material):
            return True
    for delta in _evidence_items(workflow_state, "evidence_deltas"):
        provenance = _mapping(delta.get("provenance"))
        source_id = str(provenance.get("source_id") or provenance.get("tool_call_id") or "")
        if source_id in prior:
            continue
        if _as_list(delta.get("confirmed_facts")) or _as_list(delta.get("constraints")):
            text = _safe_json(delta).lower()
            if not material or any(term in text for term in material):
                return True
    return False


def _fingerprint_terms(fp: ExperimentFingerprint) -> list[str]:
    terms: list[str] = [fp.action_type]
    data = fp.normalized_input
    for key in ("path", "host"):
        value = data.get(key)
        if isinstance(value, str) and value:
            terms.extend(part for part in re.split(r"[^a-zA-Z0-9_.-]+", value.lower()) if len(part) >= 3)
    terms.extend(str(name).lower() for name in _as_list(data.get("param_names")))
    return _dedupe(terms)


def _path_seen(path: str, workflow_state: Mapping[str, Any]) -> bool:
    if not path:
        return False
    for item in _evidence_items(workflow_state, "confirmed_facts") + _evidence_items(workflow_state, "observations"):
        if path.lower() in _safe_json(item).lower():
            return True
    return False


def _tests_active_hypothesis(data: Mapping[str, Any], workflow_state: Mapping[str, Any]) -> bool:
    current = str(workflow_state.get("current_hypothesis") or "").lower()
    text = _safe_json(data).lower()
    for item in _as_list(workflow_state.get("hypotheses")) + _as_list(workflow_state.get("candidate_chains")):
        hyp = _mapping(item)
        if current and current not in {str(hyp.get("name") or "").lower(), str(hyp.get("id") or "").lower()}:
            continue
        material = " ".join(str(hyp.get(key) or "") for key in ("claim", "falsification_test", "precondition", "preconditions"))
        tokens = [token for token in re.split(r"[^a-zA-Z0-9_]+", material.lower()) if len(token) >= 5]
        if tokens and any(token in text for token in tokens):
            return True
    return False


def _addresses_unknown(data: Mapping[str, Any], workflow_state: Mapping[str, Any]) -> bool:
    text = _safe_json(data).lower()
    for unknown in _as_list(workflow_state.get("unknowns")):
        tokens = [token for token in re.split(r"[^a-zA-Z0-9_]+", str(unknown).lower()) if len(token) >= 5]
        if tokens and any(token in text for token in tokens):
            return True
    return False


def _has_risk_refusal(items: list[dict[str, Any]]) -> bool:
    return any(str(item.get("kind") or "").lower() in {"risk_refused", "authorization_or_risk_block", "tool_blocked"} for item in items)


def _evidence_items(workflow_state: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    return [_mapping(item) for item in _as_list(workflow_state.get(name))]


def _experiment_data(experiment: Any) -> dict[str, Any]:
    data = _mapping(experiment)
    plan = _mapping(data.get("plan"))
    if plan:
        merged = {**plan, **{key: value for key, value in data.items() if key not in {"plan"}}}
        return _json_safe(redact_value(merged))
    return _json_safe(redact_value(data))


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if hasattr(value, "model_dump"):
        return _mapping(value.model_dump(mode="json"))
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


def _normalize_path(value: Any) -> str:
    path = redact_string(str(value or "")).replace("\\", "/").strip()
    normalized = posixpath.normpath(path)
    if normalized == ".":
        return ""
    while normalized.startswith("../"):
        normalized = normalized[3:]
    return normalized.lstrip("/")


def _normalize_url_path(value: str) -> str:
    path = posixpath.normpath(value or "/")
    return "/" if path == "." else path


def _safe_text(value: Any, limit: int = _TEXT_LIMIT) -> str:
    text = redact_string(str(redact_value(value))).replace("Authorization", REDACTION).replace("Bearer", REDACTION)
    return text[:limit]


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _safe_json(value: Any) -> str:
    return json.dumps(_json_safe(redact_value(value)), ensure_ascii=False, sort_keys=True, default=str)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(key):
                result[str(key)] = REDACTION if item not in (None, "", [], {}) else item
            else:
                result[str(key)] = _json_safe(item)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value, 2000)
    return value


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, round(float(value), 4)))


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
