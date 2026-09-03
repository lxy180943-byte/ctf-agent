from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ctf_agent.core.config import get_nested


@dataclass(frozen=True)
class LLMMessage:
    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class LLMResponse:
    content: str
    model: str
    provider: str
    raw: dict[str, Any] = field(default_factory=dict)


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def complete(
        self,
        messages: list[LLMMessage],
        *,
        response_format: str = "json_object",
        temperature: float = 0.0,
        max_tokens: int = 1200,
    ) -> LLMResponse:
        raise NotImplementedError


class DummyProvider(LLMProvider):
    name = "dummy"

    def __init__(self, responses: list[str] | None = None, model: str = "dummy-model") -> None:
        self.responses = list(responses or [])
        self.model = model
        self.calls: list[list[LLMMessage]] = []

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        response_format: str = "json_object",
        temperature: float = 0.0,
        max_tokens: int = 1200,
    ) -> LLMResponse:
        self.calls.append(messages)
        content = self.responses.pop(0) if self.responses else '{"rationale":"dummy fallback","commands":[]}'
        return LLMResponse(content=content, model=self.model, provider=self.name, raw={"response_format": response_format})


class OpenAICompatibleProvider(LLMProvider):
    name = "openai-compatible"

    def __init__(self, base_url: str, api_key: str, model: str, *, timeout: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        response_format: str = "json_object",
        temperature: float = 0.0,
        max_tokens: int = 1200,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [message.to_dict() for message in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = {"type": response_format}

        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        request.add_header("Accept", "application/json")
        request.add_header("Content-Type", "application/json")
        if self.api_key:
            request.add_header("Authorization", f"Bearer {self.api_key}")

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - user-configured CTF agent endpoint.
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = _read_error_body(exc)
            raise RuntimeError(f"LLM request failed with HTTP {exc.code}: {_redact(body, self.api_key)}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM request failed: {_redact(str(exc.reason), self.api_key)}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError(f"LLM request timed out after {self.timeout}s") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"LLM response was not valid JSON: {exc.msg}") from exc

        try:
            content = raw["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("LLM response missing choices[0].message.content") from exc
        return LLMResponse(content=str(content), model=self.model, provider=self.name, raw=raw)


def build_provider(config: dict[str, Any], environ: dict[str, str] | None = None) -> LLMProvider | None:
    environ = environ or os.environ
    provider_name = environ.get("CTF_AGENT_LLM_PROVIDER") or get_nested(config, ("llm", "provider"))
    provider_name = str(provider_name or "none").lower()
    if provider_name in {"none", "disabled", "off", "dry-run", "fallback"}:
        return None
    if provider_name == "dummy":
        return DummyProvider()
    if provider_name in {"openai", "openai-compatible", "openai_compatible"}:
        base_url = environ.get("OPENAI_BASE_URL") or ("https://api.openai.com/v1" if provider_name == "openai" else None)
        api_key = environ.get("OPENAI_API_KEY")
        model = environ.get("OPENAI_MODEL")
        timeout = environ.get("CTF_AGENT_LLM_TIMEOUT") or get_nested(config, ("llm", "timeout_seconds")) or 60
        missing = []
        if not base_url:
            missing.append("OPENAI_BASE_URL")
        if not model:
            missing.append("OPENAI_MODEL")
        if not api_key:
            missing.append("OPENAI_API_KEY")
        if missing:
            raise ValueError(f"OpenAI-compatible LLM requires environment variables: {', '.join(missing)}")
        return OpenAICompatibleProvider(base_url=str(base_url), api_key=str(api_key), model=str(model), timeout=int(timeout))
    raise ValueError(f"Unknown LLM provider: {provider_name}")


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")[:2000]
    except Exception:
        return str(exc)


def _redact(text: str, *secrets: str | None) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted
