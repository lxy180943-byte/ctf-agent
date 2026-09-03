"""Pure EvidenceDelta extraction from executed graph observations."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from ctf_agent.core.redaction import REDACTION, is_sensitive_key, redact_string, redact_value
from ctf_agent.core.trace import summarize_text

_TEXT_LIMIT = 700
_ALLOWED_CHALLENGE_FIELDS = {"id", "title", "category", "description", "files", "connection", "hints", "flag_regex", "metadata"}


class EvidenceProvenance(BaseModel):
    """Traceable origin for derived evidence."""

    model_config = ConfigDict(extra="forbid")

    source_type: str
    source_id: str | None = None
    tool_call_id: str | None = None
    artifact_path: str | None = None
    observation_index: int | None = None


class EvidenceDelta(BaseModel):
    """Evidence updates derived from real experiment observations only."""

    model_config = ConfigDict(extra="forbid")

    confirmed_facts: list[dict[str, Any]] = Field(default_factory=list)
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    anomalies: list[dict[str, Any]] = Field(default_factory=list)
    candidate_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    provenance: EvidenceProvenance
    extraction_notes: list[str] = Field(default_factory=list)


def derive_evidence_delta(experiment: Any, observation: Any, tool_call: Any) -> EvidenceDelta:
    """Derive evidence without running tools, models, or network calls."""

    exp = _mapping(experiment)
    obs = _mapping(observation)
    call = _mapping(tool_call)
    evidence = _evidence(obs)
    action_type = _safe_text(call.get("action_type") or exp.get("action_type") or obs.get("source") or "unknown")
    provenance = EvidenceProvenance(
        source_type=action_type,
        source_id=_safe_text(exp.get("id") or call.get("experiment_id") or call.get("id") or obs.get("source")),
        tool_call_id=_safe_text(call.get("id") or call.get("tool_call_id") or call.get("experiment_id")),
        artifact_path=_artifact_path(call, obs, evidence),
        observation_index=_observation_index(obs),
    )
    confirmed: list[dict[str, Any]] = []
    constraints: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    notes: list[str] = []

    if action_type == "read_file":
        _read_file(evidence, call, provenance, confirmed, constraints, artifacts)
    elif action_type == "http_request":
        _http_request(evidence, call, provenance, confirmed, constraints, artifacts)
    elif action_type == "inspect_binary":
        _inspect_binary(evidence, call, provenance, confirmed, constraints, artifacts)
    elif action_type == "search_artifacts":
        _search_artifacts(evidence, call, provenance, confirmed, artifacts)
    elif action_type in {"ask_verifier", "verifier"} or _safe_text(obs.get("source")).lower() == "verifier":
        _verifier(evidence, obs, provenance, confirmed, artifacts)
    else:
        notes.append(f"No confirmed fact extractor for action_type={action_type}")

    _challenge_metadata(exp, obs, provenance, confirmed)
    _generic_constraints(obs, call, provenance, constraints)
    _generic_anomalies(exp, obs, call, evidence, provenance, anomalies)
    if _contains_advisory_only(exp) or _contains_advisory_only(obs) or _contains_advisory_only(call):
        notes.append("Ignored advisory model, memory, skill, hypothesis, or candidate chain text as confirmed evidence.")
    if _mentions_unverified_flag(exp) or _mentions_unverified_flag(obs) or _mentions_unverified_flag(call):
        notes.append("Ignored unverified flag-like text because verifier confirmation is required.")

    return EvidenceDelta(
        confirmed_facts=_dedupe(confirmed),
        constraints=_dedupe(constraints),
        anomalies=_dedupe(anomalies),
        candidate_artifacts=_dedupe(artifacts),
        provenance=provenance,
        extraction_notes=sorted(set(notes)),
    )


def _read_file(evidence, call, provenance, confirmed, constraints, artifacts) -> None:
    path = evidence.get("path") or _nested(call, "action_input", "path")
    if path:
        confirmed.append(_item("read_file_path", f"read_file succeeded for {_safe_text(path)}", provenance, path=path))
        artifacts.append(_item("file_artifact", f"read file artifact {_safe_text(path)}", provenance, path=path))
    bytes_read = evidence.get("bytes_read")
    if bytes_read is not None:
        confirmed.append(_item("read_file_bytes", f"read_file read {bytes_read} bytes", provenance, bytes_read=bytes_read))
    if evidence.get("truncated") is not None:
        confirmed.append(_item("read_file_truncated", f"read_file truncated={bool(evidence.get("truncated"))}", provenance, truncated=bool(evidence.get("truncated"))))
    for key in ("status", "title", "body_excerpt"):
        value = evidence.get(key)
        if value not in (None, "", [], {}):
            confirmed.append(_item(f"read_file_{key}", f"read_file observed {key}: {_safe_text(value)}", provenance, **{key: value}))
    if evidence.get("php_analysis"):
        _php_constraints(evidence.get("php_analysis"), provenance, constraints)


def _http_request(evidence, call, provenance, confirmed, constraints, artifacts) -> None:
    status = evidence.get("status")
    if status is not None:
        confirmed.append(_item("http_status", f"HTTP status {status}", provenance, status=status))
    headers = evidence.get("headers")
    if headers:
        confirmed.append(_item("http_headers", "HTTP headers were observed", provenance, headers=headers))
    for key in ("forms", "links", "scripts"):
        value = evidence.get(key)
        if value:
            confirmed.append(_item(f"http_{key}", f"HTTP {key} observed", provenance, **{key: value}))
    request = evidence.get("request") if isinstance(evidence.get("request"), Mapping) else {}
    if request:
        constraints.append(_item("http_network_scope", "HTTP request was scoped by method URL params and header names", provenance, request=request))
    if evidence.get("php_analysis"):
        _php_constraints(evidence.get("php_analysis"), provenance, constraints)
    for path in _as_list(evidence.get("artifact_paths")) + _as_list(call.get("artifact_paths")):
        artifacts.append(_item("http_artifact", f"HTTP artifact {_safe_text(path)}", provenance, path=path))


def _inspect_binary(evidence, call, provenance, confirmed, constraints, artifacts) -> None:
    path = evidence.get("path") or _nested(call, "action_input", "path")
    if path:
        confirmed.append(_item("binary_path", f"inspected binary {_safe_text(path)}", provenance, path=path))
        artifacts.append(_item("binary_artifact", f"binary artifact {_safe_text(path)}", provenance, path=path))
    binary = evidence.get("binary") if isinstance(evidence.get("binary"), Mapping) else {}
    for key in ("format", "file_type", "arch", "bits", "endianness", "sha256"):
        value = binary.get(key)
        if value not in (None, "", [], {}):
            confirmed.append(_item(f"binary_{key}", f"binary {key}: {_safe_text(value)}", provenance, **{key: value}))
    protections = binary.get("protections") if isinstance(binary.get("protections"), Mapping) else {}
    for key, value in sorted(protections.items()):
        constraints.append(_item("binary_protection", f"binary protection {_safe_text(key)}={_safe_text(value)}", provenance, protection=key, value=value))
    for key in ("nx", "pie", "canary", "relro", "stripped"):
        if key in binary:
            constraints.append(_item("binary_protection", f"binary protection {key}={_safe_text(binary.get(key))}", provenance, protection=key, value=binary.get(key)))


def _search_artifacts(evidence, call, provenance, confirmed, artifacts) -> None:
    pattern = evidence.get("pattern") or _nested(call, "action_input", "pattern")
    if pattern:
        confirmed.append(_item("search_pattern", f"searched artifacts for {_safe_text(pattern)}", provenance, pattern=pattern))
    if evidence.get("match_count") is not None:
        confirmed.append(_item("search_match_count", f"search_artifacts found {evidence.get("match_count")} matches", provenance, match_count=evidence.get("match_count")))
    for match in _as_list(evidence.get("matches")):
        data = _mapping(match)
        path = data.get("path")
        if path:
            line = data.get("line")
            confirmed.append(_item("search_match", f"match in {_safe_text(path)} line {_safe_text(line)}", provenance, path=path, line=line))
            artifacts.append(_item("search_match_artifact", f"matched artifact {_safe_text(path)}", provenance, path=path, line=line))


def _verifier(evidence, obs, provenance, confirmed, artifacts) -> None:
    candidates = _as_list(evidence.get("candidates") or evidence.get("verified_candidates") or obs.get("verified_candidates"))
    for candidate in candidates:
        data = _mapping(candidate)
        if data.get("verified") is True and (data.get("value") or data.get("candidate")):
            value = data.get("value") or data.get("candidate")
            confirmed.append(_item("verified_candidate", "verifier confirmed a candidate", provenance, candidate=value, source=data.get("source")))
            artifacts.append(_item("verified_candidate", "verified candidate available", provenance, candidate=value, source=data.get("source")))
    if evidence.get("verified_count"):
        confirmed.append(_item("verified_count", f"verifier confirmed {evidence.get("verified_count")} candidates", provenance, verified_count=evidence.get("verified_count")))


def _challenge_metadata(exp, obs, provenance, confirmed) -> None:
    for source in (exp.get("challenge"), obs.get("challenge"), exp.get("challenge_metadata"), obs.get("challenge_metadata")):
        data = _mapping(source)
        for key, value in sorted(data.items()):
            if key in _ALLOWED_CHALLENGE_FIELDS and value not in (None, "", [], {}):
                confirmed.append(_item("challenge_metadata", f"challenge {key}: {_safe_text(value)}", provenance, field=key, value=value))


def _php_constraints(analysis, provenance, constraints) -> None:
    data = _mapping(analysis)
    for parameter in _as_list(data.get("parameters")):
        constraints.append(_item("php_parameter", f"PHP parameter {_safe_text(parameter)}", provenance, parameter=parameter))
    for sink in _as_list(data.get("sinks") or data.get("dangerous_functions")):
        constraints.append(_item("php_sink", f"PHP sink {_safe_text(sink)}", provenance, sink=sink))
    for comparison in _as_list(data.get("guards") or data.get("comparisons")):
        constraints.append(_item("php_comparison", f"PHP comparison {_safe_text(comparison)}", provenance, comparison=comparison))
    for pattern in _as_list(data.get("blacklist") or data.get("blacklist_patterns")):
        constraints.append(_item("php_blacklist", f"PHP blacklist {_safe_text(pattern)}", provenance, pattern=pattern))
    for include in _as_list(data.get("include_points")):
        constraints.append(_item("php_include", f"PHP include {_safe_text(include)}", provenance, include=include))


def _generic_constraints(obs, call, provenance, constraints) -> None:
    for key in ("authorization", "risk", "risk_decision"):
        value = obs.get(key) if key in obs else call.get(key)
        if value not in (None, "", [], {}):
            constraints.append(_item(key, f"{key} constraint observed", provenance, value=value))
    if call.get("status") == "blocked":
        constraints.append(_item("tool_blocked", "tool execution was blocked", provenance, reason=call.get("risk_decision") or obs.get("error")))
    if call.get("failure_signal_matched"):
        constraints.append(_item("failure_signal", "failure signal matched", provenance, failure_signal=call.get("failure_signal")))


def _generic_anomalies(exp, obs, call, evidence, provenance, anomalies) -> None:
    status = _safe_text(call.get("status")).lower()
    if obs.get("ok") is False or status in {"failed", "blocked"}:
        anomalies.append(_item("tool_failure", f"tool status {status or "not ok"}", provenance, error=obs.get("error"), status=status))
    exit_code = evidence.get("exit_code")
    if exit_code not in (None, 0):
        anomalies.append(_item("nonzero_exit", f"exit_code {exit_code}", provenance, exit_code=exit_code))
    if evidence.get("timed_out") or obs.get("timed_out") or call.get("timed_out"):
        anomalies.append(_item("timeout", "tool execution timed out", provenance))
    if call.get("expected_signal_matched") is False and call.get("expected_signal"):
        anomalies.append(_item("expected_signal_missing", "expected signal was not observed", provenance, expected_signal=call.get("expected_signal")))
    if call.get("failure_signal_matched") is True:
        anomalies.append(_item("failure_signal_matched", "failure signal was observed", provenance, failure_signal=call.get("failure_signal")))
    http_status = evidence.get("status")
    if isinstance(http_status, int) and http_status >= 400:
        anomalies.append(_item("http_unexpected_status", f"HTTP status {http_status}", provenance, status=http_status))
    risk_text = json.dumps(_json_safe({"obs": obs, "call": call}), sort_keys=True).lower()
    if any(word in risk_text for word in ("denied", "blocked", "refuse", "unauthorized", "outside")):
        anomalies.append(_item("authorization_or_risk_block", "risk or authorization blocked execution", provenance, details={"authorization": obs.get("authorization"), "risk": obs.get("risk"), "risk_decision": call.get("risk_decision")}))
    failure_signal = _nested(exp, "plan", "failure_signal") or call.get("failure_signal")
    evidence_text = json.dumps(_json_safe(evidence), sort_keys=True).lower()
    if failure_signal and _safe_text(failure_signal).lower() in evidence_text:
        anomalies.append(_item("failure_signal_in_observation", "failure signal text appeared in observation", provenance, failure_signal=failure_signal))


def _evidence(observation: Mapping[str, Any]) -> dict[str, Any]:
    nested = observation.get("evidence")
    return _mapping(nested) if isinstance(nested, Mapping) else dict(observation)


def _item(kind: str, summary: str, provenance: EvidenceProvenance, **data: Any) -> dict[str, Any]:
    return {"kind": _safe_text(kind), "summary": _safe_text(summary), "data": _json_safe(data), "provenance": provenance.model_dump(mode="json")}


def _artifact_path(call: Mapping[str, Any], obs: Mapping[str, Any], evidence: Mapping[str, Any]) -> str | None:
    for value in _as_list(call.get("artifact_paths")) + _as_list(obs.get("artifact_paths")):
        return _safe_text(value)
    if evidence.get("path"):
        return _safe_text(evidence.get("path"))
    return None


def _observation_index(obs: Mapping[str, Any]) -> int | None:
    value = obs.get("observation_index") if obs.get("observation_index") is not None else obs.get("index")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


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


def _nested(value: Mapping[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _safe_text(value: Any) -> str:
    text = str(redact_value(_plain(value)))
    text = redact_string(text)
    text = re.sub(r"(?i)authorization\s*[:=]\s*(?:(?:bearer|token)\s*)?<redacted>", REDACTION, text)
    text = re.sub(r"(?i)\b(?:bearer|token)\s+<redacted>", REDACTION, text)
    text = re.sub(r"(?i)\bauthorization\b", REDACTION, text)
    text = re.sub(r"(?i)\bbearer\b", REDACTION, text)
    text = re.sub(r"(?i)api[_ -]?key", REDACTION, text)
    return summarize_text(text, limit=_TEXT_LIMIT) or ""


def _json_safe(value: Any) -> Any:
    safe = _drop_sensitive(redact_value(_plain(value)))
    return json.loads(json.dumps(_limit_strings(safe), ensure_ascii=False, default=str))


def _drop_sensitive(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _drop_sensitive(item) for key, item in value.items() if not is_sensitive_key(key)}
    if isinstance(value, list):
        return [_drop_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [_drop_sensitive(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value)
    return value


def _limit_strings(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, Mapping):
        return {str(key): _limit_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_limit_strings(item) for item in value]
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    return value


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _contains_advisory_only(value: Any) -> bool:
    text = json.dumps(_json_safe(value), sort_keys=True).lower()
    return any(word in text for word in ("hypothesis", "candidate_chains", "memory", "skill_notes", "skill note", "advisory"))


def _mentions_unverified_flag(value: Any) -> bool:
    text = json.dumps(_json_safe(value), sort_keys=True).lower()
    if not re.search(r"(?:flag|ctf)\{[^}\n]{1,512}\}", text, re.I):
        return False
    return "verified" not in text or "false" in text or "model" in text
