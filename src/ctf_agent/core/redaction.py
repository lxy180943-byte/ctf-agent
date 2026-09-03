from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTION = "<redacted>"

SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "passwd",
    "password",
    "secret",
    "token",
)

SECRET_ENV_NAMES = {
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "CTF_AGENT_CTFD_TOKEN",
    "OPENAI_API_KEY",
}

SECRET_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")

AUTH_HEADER_RE = re.compile(r"(?i)\b(authorization\s*[:=]\s*)(bearer|token)\s+[^\s'\";,]+")
TOKEN_ASSIGNMENT_RE = re.compile(r"(?i)\b(api[_-]?key|token|secret|password)=([^\s&]+)")
BEARER_RE = re.compile(r"(?i)\b(bearer|token)\s+([A-Za-z0-9._~+/=-]{12,})")


def sensitive_env_values(environ: Mapping[str, str] | None = None) -> list[str]:
    environ = environ or os.environ
    values: list[str] = []
    for name, value in environ.items():
        upper = name.upper()
        if not value or len(value) < 4:
            continue
        if upper in SECRET_ENV_NAMES or upper.startswith("CTF_AGENT_CTFD_") and upper.endswith("_TOKEN") or upper.endswith(SECRET_ENV_SUFFIXES):
            values.append(value)
    return sorted(set(values), key=len, reverse=True)


def is_sensitive_key(key: object) -> bool:
    lowered = str(key).lower().replace("-", "_")
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def redact_string(value: str, secrets: Sequence[str] | None = None) -> str:
    redacted = value
    for secret in secrets if secrets is not None else sensitive_env_values():
        if secret:
            redacted = redacted.replace(secret, REDACTION)
    redacted = AUTH_HEADER_RE.sub(lambda match: match.group(1) + match.group(2) + " " + REDACTION, redacted)
    redacted = TOKEN_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}={REDACTION}", redacted)
    redacted = BEARER_RE.sub(lambda match: f"{match.group(1)} {REDACTION}", redacted)
    return redacted


def redact_value(value: Any, *, key: object | None = None, secrets: Sequence[str] | None = None) -> Any:
    secrets = list(secrets) if secrets is not None else sensitive_env_values()
    if key is not None and is_sensitive_key(key):
        if value in (None, "", [], {}):
            return value
        return REDACTION
    if isinstance(value, str):
        return redact_string(value, secrets)
    if isinstance(value, Mapping):
        return {str(item_key): redact_value(item_value, key=item_key, secrets=secrets) for item_key, item_value in value.items()}
    if isinstance(value, tuple):
        return tuple(redact_value(item, secrets=secrets) for item in value)
    if isinstance(value, list):
        return [redact_value(item, secrets=secrets) for item in value]
    return value
