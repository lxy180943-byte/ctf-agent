from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_RELATIVE = Path("configs/default.yaml")

SECRET_CONFIG_KEYS = {"api_key", "apikey", "authorization", "password", "secret", "token"}
NON_SECRET_CONFIG_KEYS = {"max_tokens", "token_limit", "token_budget"}
LLM_ENV_ONLY_CONFIG_KEYS = {"api_key", "base_url", "model"}


ENV_OVERRIDES = {
    "CTF_AGENT_WORKSPACE_DIR": ("workspace_dir",),
    "CTF_AGENT_ARTIFACTS_DIR": ("artifacts_dir",),
    "CTF_AGENT_LOG_LEVEL": ("logging", "level"),
    "CTF_AGENT_TRACE_ENABLED": ("logging", "trace_enabled"),
    "CTF_AGENT_TRACE_PATH": ("logging", "trace_path"),
    "CTF_AGENT_SUBMIT_ENABLED": ("submit", "enabled"),
    "CTF_AGENT_DOCKER_IMAGE": ("sandbox", "image"),
    "CTF_AGENT_DOCKER_NETWORK": ("sandbox", "network"),
    "CTF_AGENT_ALLOW_NETWORK": ("sandbox", "allow_network"),
    "CTF_AGENT_CTFD_URL": ("platform", "ctfd", "url"),
    "CTF_AGENT_CTFD_TOKEN": ("platform", "ctfd", "token"),
    "CTF_AGENT_LLM_PROVIDER": ("llm", "provider"),
    "CTF_AGENT_LLM_TIMEOUT": ("llm", "timeout_seconds"),
    "CTF_AGENT_LLM_MAX_TOKENS": ("llm", "max_tokens"),
    "CTF_AGENT_ORCHESTRATION_MODE": ("orchestration", "mode"),
    "CTF_AGENT_CRITIC_AFTER_FAILURES": ("orchestration", "critic_after_failures"),
    "CTF_AGENT_MEMORY_ENABLED": ("memory", "enabled"),
    "CTF_AGENT_MEMORY_PATH": ("memory", "path"),
    "CTF_AGENT_MEMORY_AUTO_LEARN": ("memory", "auto_learn"),
    "CTF_AGENT_MEMORY_SEARCH_LIMIT": ("memory", "search_limit"),
}


class ConfigError(ValueError):
    """Raised when the local configuration cannot be loaded."""


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_config_path() -> Path:
    cwd_candidate = Path.cwd() / DEFAULT_CONFIG_RELATIVE
    if cwd_candidate.exists():
        return cwd_candidate
    return project_root() / DEFAULT_CONFIG_RELATIVE


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "~"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        body = value[1:-1].strip()
        if not body:
            return []
        return [parse_scalar(part.strip()) for part in body.split(",")]
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _meaningful_lines(lines: list[str], start: int) -> tuple[int, str] | None:
    for index in range(start, len(lines)):
        candidate = lines[index]
        if candidate.strip() and not candidate.lstrip().startswith("#"):
            return index, candidate
    return None


def parse_simple_yaml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any] | list[Any]]] = [(-1, root)]
    lines = text.splitlines()

    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent % 2 != 0:
            raise ConfigError(f"Invalid indentation on line {line_number}: use multiples of two spaces")
        line = raw_line.strip()

        while indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if line.startswith("- "):
            if not isinstance(parent, list):
                raise ConfigError(f"Invalid list item on line {line_number}: parent is not a list")
            item = line[2:].strip()
            if not item:
                raise ConfigError(f"Invalid empty list item on line {line_number}")
            parent.append(parse_scalar(item))
            continue

        if ":" not in line:
            raise ConfigError(f"Invalid config line {line_number}: expected key: value")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            raise ConfigError(f"Invalid empty key on line {line_number}")

        if not isinstance(parent, dict):
            raise ConfigError(f"Invalid mapping item on line {line_number}: parent is not a mapping")

        if raw_value == "":
            next_line = _meaningful_lines(lines, line_number)
            if next_line is None:
                parent[key] = None
                continue
            _, next_raw = next_line
            next_indent = len(next_raw) - len(next_raw.lstrip(" "))
            if next_indent <= indent:
                parent[key] = None
                continue
            child: dict[str, Any] | list[Any] = [] if next_raw.strip().startswith("- ") else {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = parse_scalar(raw_value)

    return root


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def get_nested(config: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = config
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def set_nested(config: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = config
    for part in path[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[path[-1]] = value


def coerce_env_value(raw_value: str, existing_value: Any) -> Any:
    if isinstance(existing_value, bool):
        return raw_value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(existing_value, int) and not isinstance(existing_value, bool):
        return int(raw_value)
    if isinstance(existing_value, float):
        return float(raw_value)
    return parse_scalar(raw_value)


def apply_env_overrides(config: dict[str, Any], environ: dict[str, str] | None = None) -> dict[str, Any]:
    environ = environ or os.environ
    merged = deepcopy(config)

    for env_name, path in ENV_OVERRIDES.items():
        if env_name in environ:
            existing = get_nested(merged, path)
            set_nested(merged, path, coerce_env_value(environ[env_name], existing))

    prefix = "CTF_AGENT_CONFIG__"
    for env_name, raw_value in environ.items():
        if not env_name.startswith(prefix):
            continue
        path = tuple(part.lower() for part in env_name[len(prefix) :].split("__") if part)
        if not path:
            continue
        existing = get_nested(merged, path)
        set_nested(merged, path, coerce_env_value(raw_value, existing))

    return merged


def expand_paths(config: dict[str, Any]) -> dict[str, Any]:
    expanded = deepcopy(config)
    for path in (("workspace_dir",), ("artifacts_dir",), ("logging", "trace_path"), ("memory", "path"), ("knowledge", "skill_docs")):
        value = get_nested(expanded, path)
        if isinstance(value, str):
            set_nested(expanded, path, str(Path(value).expanduser()))
    return expanded


def validate_llm_env_only_fields(config: dict[str, Any]) -> None:
    llm = config.get("llm")
    if not isinstance(llm, dict):
        return
    forbidden = sorted(str(key) for key in llm if str(key).lower().replace("-", "_") in LLM_ENV_ONLY_CONFIG_KEYS)
    if forbidden:
        joined = ", ".join(f"llm.{key}" for key in forbidden)
        raise ConfigError(
            "OpenAI connection settings are environment-only. "
            "Use OPENAI_API_KEY, OPENAI_BASE_URL, and OPENAI_MODEL instead of config fields: "
            f"{joined}"
        )


def validate_secret_sources(config: dict[str, Any], config_path: str | Path) -> None:
    validate_llm_env_only_fields(config)
    path = Path(config_path).expanduser()
    if _is_local_secret_config(path):
        return
    findings = list(_find_plaintext_secrets(config))
    if findings:
        joined = ", ".join(findings[:5])
        raise ConfigError(
            "Plaintext secrets are only allowed in environment variables or local ignored config files "
            f"(*.local.yaml, *private*.yaml, *secret*.yaml, configs/ctfd*.yaml). Found: {joined}"
        )


def _is_local_secret_config(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.startswith(".")
        or name.endswith(".local.yaml")
        or "private" in name
        or "secret" in name
        or name.startswith("ctfd")
        or (path.parent.name == "configs" and name.startswith("ctfd"))
    )


def _find_plaintext_secrets(value: Any, prefix: tuple[str, ...] = ()) -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = (*prefix, str(key))
            lowered = str(key).lower().replace("-", "_")
            if lowered in NON_SECRET_CONFIG_KEYS:
                continue
            if lowered in SECRET_CONFIG_KEYS or any(part in lowered for part in SECRET_CONFIG_KEYS):
                if item not in (None, "", [], {}):
                    findings.append(".".join(path))
                    continue
            findings.extend(_find_plaintext_secrets(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(_find_plaintext_secrets(item, (*prefix, str(index))))
    return findings


def load_config(path: str | Path | None = None, environ: dict[str, str] | None = None) -> dict[str, Any]:
    environ = environ or os.environ
    if path:
        config_path = Path(path).expanduser()
    elif environ.get("CTF_AGENT_CONFIG"):
        config_path = Path(environ["CTF_AGENT_CONFIG"]).expanduser()
    else:
        config_path = default_config_path()
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")
    config = parse_simple_yaml(config_path.read_text(encoding="utf-8"))
    validate_secret_sources(config, config_path)
    config = apply_env_overrides(config, environ=environ)
    validate_llm_env_only_fields(config)
    return expand_paths(config)
