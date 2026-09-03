from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ActionType(str, Enum):
    RUN_COMMAND = "run_command"
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    SEARCH_ARTIFACTS = "search_artifacts"
    ASK_VERIFIER = "ask_verifier"
    FINISH = "finish"
    PAUSE = "pause"


class ActionValidationError(ValueError):
    """Raised when an LLM action response does not match the allowed schema."""


class ActionGuardError(ValueError):
    """Raised when an otherwise valid action violates observed-state guardrails."""


@dataclass(frozen=True)
class LLMAction:
    type: ActionType
    reason: str = ""
    command: str | None = None
    path: str | None = None
    content: str | None = None
    pattern: str | None = None
    flag: str | None = None
    timeout: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type.value, "reason": self.reason}
        for key in ("command", "path", "content", "pattern", "flag", "timeout"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data


@dataclass(frozen=True)
class LLMActionDecision:
    hypothesis: str
    evidence_used: list[str]
    uncertainty: list[str]
    actions: list[LLMAction]
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def rationale(self) -> str:
        return self.hypothesis

    @property
    def next_actions(self) -> list[LLMAction]:
        return self.actions

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "hypothesis": self.hypothesis,
            "evidence_used": list(self.evidence_used),
            "uncertainty": list(self.uncertainty),
            "next_actions": [action.to_dict() for action in self.actions],
        }
        payload["rationale"] = self.hypothesis
        payload["actions"] = [action.to_dict() for action in self.actions]
        return payload


ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["hypothesis", "evidence_used", "uncertainty", "next_actions"],
    "properties": {
        "hypothesis": {"type": "string"},
        "evidence_used": {"type": "array", "items": {"type": "string"}},
        "uncertainty": {"type": "array", "items": {"type": "string"}},
        "next_actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"enum": [item.value for item in ActionType]},
                    "reason": {"type": "string"},
                    "command": {"type": "string"},
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "pattern": {"type": "string"},
                    "flag": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
                    "metadata": {"type": "object"},
                },
            },
        },
        "rationale": {"type": "string"},
        "actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"enum": [item.value for item in ActionType]},
                    "reason": {"type": "string"},
                    "command": {"type": "string"},
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "pattern": {"type": "string"},
                    "flag": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
                    "metadata": {"type": "object"},
                },
            },
        },
    },
}

_ALLOWED_FIELDS = {"type", "reason", "command", "path", "content", "pattern", "flag", "timeout", "metadata"}
_REQUIRED_BY_ACTION: dict[ActionType, set[str]] = {
    ActionType.RUN_COMMAND: {"command"},
    ActionType.READ_FILE: {"path"},
    ActionType.WRITE_FILE: {"path", "content"},
    ActionType.SEARCH_ARTIFACTS: {"pattern"},
    ActionType.ASK_VERIFIER: set(),
    ActionType.FINISH: set(),
    ActionType.PAUSE: set(),
}


def parse_json_object(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("LLM action response must be a JSON object")
    return data


def parse_strict_json_object(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ActionValidationError(f"LLM response must be strict JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ActionValidationError("LLM response must be a JSON object")
    return data


def validate_action_response(data: dict[str, Any], *, max_actions: int = 3) -> LLMActionDecision:
    hypothesis = _optional_text(data.get("hypothesis")) or _optional_text(data.get("rationale"))
    if not hypothesis:
        raise ActionValidationError("Action response requires a non-empty string hypothesis")

    evidence_used = _string_list(data.get("evidence_used"), field_name="evidence_used")
    uncertainty = _string_list(data.get("uncertainty"), field_name="uncertainty")

    raw_actions = data.get("next_actions", data.get("actions"))
    if not isinstance(raw_actions, list):
        raise ActionValidationError("Action response requires next_actions/actions as a list")
    if not 1 <= len(raw_actions) <= max_actions:
        raise ActionValidationError(f"Action response must contain 1-{max_actions} actions")

    actions: list[LLMAction] = []
    for index, item in enumerate(raw_actions, start=1):
        if not isinstance(item, dict):
            raise ActionValidationError(f"Action {index} must be an object")
        unknown_fields = set(item) - _ALLOWED_FIELDS
        if unknown_fields:
            raise ActionValidationError(f"Action {index} contains unknown field(s): {sorted(unknown_fields)}")
        try:
            action_type = ActionType(str(item.get("type")))
        except ValueError as exc:
            raise ActionValidationError(f"Action {index} has unknown type: {item.get('type')}") from exc
        for field_name in _REQUIRED_BY_ACTION[action_type]:
            if not isinstance(item.get(field_name), str) or not str(item[field_name]).strip():
                raise ActionValidationError(f"Action {index} type {action_type.value} requires non-empty {field_name}")
        timeout = item.get("timeout")
        if timeout is not None and (not isinstance(timeout, int) or timeout < 1 or timeout > 600):
            raise ActionValidationError(f"Action {index} timeout must be an integer from 1 to 600")
        metadata = item.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ActionValidationError(f"Action {index} metadata must be an object")
        actions.append(
            LLMAction(
                type=action_type,
                reason=str(item.get("reason") or ""),
                command=_optional_str(item.get("command")),
                path=_optional_str(item.get("path")),
                content=_optional_str(item.get("content")),
                pattern=_optional_str(item.get("pattern")),
                flag=_optional_str(item.get("flag")),
                timeout=timeout,
                metadata=dict(metadata),
            )
        )
    return LLMActionDecision(hypothesis=hypothesis, evidence_used=evidence_used, uncertainty=uncertainty, actions=actions, raw=data)


def parse_action_decision(text: str, *, max_actions: int = 3) -> LLMActionDecision:
    return validate_action_response(parse_strict_json_object(text), max_actions=max_actions)


def ensure_relative_workspace_path(value: str, *, root: Path, allow_missing: bool = False) -> Path:
    if not value or "\x00" in value:
        raise ActionGuardError("Path must be a non-empty relative workspace path")
    raw = Path(value).expanduser()
    if raw.is_absolute():
        raise ActionGuardError(f"Absolute paths are not allowed in LLM file actions: {value}")
    root = root.expanduser().resolve()
    resolved = (root / raw).resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise ActionGuardError(f"Path escapes the allowed root: {value}")
    if not allow_missing and not resolved.exists():
        raise ActionGuardError(f"Path has not been observed or does not exist: {value}")
    return resolved


def extract_command_actions(data: dict[str, Any], *, max_actions: int = 3) -> list[dict[str, Any]]:
    raw_actions = data.get("commands", data.get("actions", []))
    if not isinstance(raw_actions, list):
        raise ValueError("LLM action response field commands/actions must be a list")
    actions: list[dict[str, Any]] = []
    for item in raw_actions[:max_actions]:
        if not isinstance(item, dict):
            continue
        command = item.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        actions.append(
            {
                "command": command.strip(),
                "reason": str(item.get("reason") or "LLM-suggested action"),
                "timeout": item.get("timeout"),
                "metadata": dict(item.get("metadata", {})) if isinstance(item.get("metadata", {}), dict) else {},
            }
        )
    return actions


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ActionValidationError("Optional string action fields must be strings when present")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ActionValidationError("Action response text fields must be strings")
    text = value.strip()
    return text or None


def _string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ActionValidationError(f"Action response field {field_name} must be a list")
    result: list[str] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str):
            raise ActionValidationError(f"Action response field {field_name}[{index}] must be a string")
        text = item.strip()
        if text:
            result.append(text)
    return result
