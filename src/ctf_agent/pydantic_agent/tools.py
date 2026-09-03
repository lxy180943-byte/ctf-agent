"""Structured, policy-bound tool adapters for the PydanticAI workflow brain."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Literal
from urllib.parse import urlencode, urlparse, urlunparse

from pydantic import BaseModel, ConfigDict, Field

from ctf_agent.agents.base import AgentContext
from ctf_agent.agents.executor import ExecutionBatch
from ctf_agent.agents.verifier import VerifierAgent
from ctf_agent.analysis.observation import ObservationSummarizer
from ctf_agent.core.models import Artifact, Observation, Step, utc_now
from ctf_agent.core.redaction import redact_value
from ctf_agent.core.state import ChallengeState
from ctf_agent.core.trace import TraceEvent
from ctf_agent.llm.risk import RiskLevel, classify_command_risk
from ctf_agent.sandbox import ExecutionResult, WorkspaceBoundaryError
from ctf_agent.sandbox.executor import resolve_inside
from ctf_agent.sandbox.network_policy import local_executor_network_note

_READ_LIMIT = 200_000
_SEARCH_FILE_LIMIT = 250_000
_MAX_SEARCH_RESULTS = 40
_NETWORK_COMMANDS = {"curl", "wget", "nc", "netcat", "nmap", "ffuf", "sqlmap", "nikto", "gobuster", "feroxbuster"}


class ToolInput(BaseModel):
    """Shared strict input contract for every tool call."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReadFileInput(ToolInput):
    path: str = Field(min_length=1, max_length=1024)


class SearchArtifactsInput(ToolInput):
    pattern: str = Field(min_length=1, max_length=512)


class RunCommandInput(ToolInput):
    command: str = Field(min_length=1, max_length=8000)
    timeout: int = Field(default=60, ge=1, le=300)


class HttpRequestInput(ToolInput):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "GET"
    url: str = Field(min_length=1, max_length=4096)
    params: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = Field(default=None, max_length=100_000)
    timeout: int = Field(default=20, ge=1, le=120)


class InspectBinaryInput(ToolInput):
    path: str = Field(min_length=1, max_length=1024)


class AskVerifierInput(ToolInput):
    pass


class PauseForHumanInput(ToolInput):
    reason: str = Field(min_length=1, max_length=2000)


class ToolObservation(BaseModel):
    """A bounded, machine-readable tool result for model context and resume."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    ok: bool
    risk: Literal["low", "medium", "high", "refuse"]
    duration_seconds: float = Field(ge=0)
    observation: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


@dataclass
class ToolDependencies:
    """Existing workflow services made available to structured tools only."""

    context: AgentContext
    observation_summarizer: ObservationSummarizer = field(default_factory=ObservationSummarizer)
    execution_batch: ExecutionBatch = field(default_factory=ExecutionBatch)


def recommended_tools(deps: ToolDependencies, *, limit: int = 10) -> list[dict[str, object]]:
    """Return registry recommendations; this function has no execution capability."""

    category = deps.context.state.challenge.category
    return [tool.to_dict() for tool in deps.context.tool_registry.recommend(category, limit=limit)]


def visible_workspace_paths(deps: ToolDependencies) -> list[str]:
    """Expose challenge-declared paths, not arbitrary host filesystem paths."""

    return sorted(set(deps.context.state.challenge.files))


def read_file(deps: ToolDependencies, request: ReadFileInput) -> ToolObservation:
    started = time.monotonic()
    try:
        path = _resolve_workspace_path(deps, request.path, must_exist=True)
        raw = path.read_bytes()[:_READ_LIMIT]
        text = raw.decode("utf-8", errors="replace")
        artifact = Artifact(path=str(path), kind="text", description=f"Structured read: {_display_path(deps, path)}")
        evidence = deps.observation_summarizer.summarize(text)
        evidence.update({"path": _display_path(deps, path), "bytes_read": len(raw), "truncated": path.stat().st_size > len(raw)})
        return _record(deps, "read_file", started, True, "low", evidence, [artifact])
    except (OSError, WorkspaceBoundaryError, ValueError) as exc:
        return _record(deps, "read_file", started, False, "refuse", {"path": request.path}, error=str(exc))


def search_artifacts(deps: ToolDependencies, request: SearchArtifactsInput) -> ToolObservation:
    started = time.monotonic()
    matches: list[dict[str, Any]] = []
    for root in (deps.context.layout.artifacts_dir, deps.context.layout.work_dir):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if len(matches) >= _MAX_SEARCH_RESULTS:
                break
            if not path.is_file():
                continue
            try:
                text = path.read_bytes()[:_SEARCH_FILE_LIMIT].decode("utf-8", errors="replace")
            except OSError:
                continue
            if request.pattern in text:
                matches.append({"path": _display_path(deps, path), "line": _line_number(text, request.pattern)})
    evidence = {"pattern": request.pattern, "matches": matches, "match_count": len(matches), "truncated": len(matches) >= _MAX_SEARCH_RESULTS}
    return _record(deps, "search_artifacts", started, True, "low", evidence)


def run_command(deps: ToolDependencies, request: RunCommandInput) -> ToolObservation:
    """Compatibility fallback. Prefer read, HTTP, binary, and artifact tools first."""

    started = time.monotonic()
    risk = classify_command_risk(request.command, deps.context.state.challenge.connection)
    if risk.level is RiskLevel.REFUSE or risk.confirm_required:
        return _record(deps, "run_command", started, False, risk.level.value, {"reason": risk.reason}, error=risk.reason)
    if _contains_network_command(request.command) and not _network_authorized(deps, deps.context.state.challenge.connection or ""):
        reason = "Network command denied: no explicit, authorized challenge connection is configured."
        return _record(deps, "run_command", started, False, "high", {"reason": reason}, error=reason)
    try:
        result = deps.context.executor.run(request.command, cwd=deps.context.layout.work_dir, timeout=request.timeout, env={})
    except (WorkspaceBoundaryError, RuntimeError, ValueError) as exc:
        return _record(deps, "run_command", started, False, risk.level.value, {"command": request.command}, error=str(exc))
    deps.execution_batch.results.append(result)
    evidence = _execution_evidence(deps, result)
    return _record(deps, "run_command", started, result.ok, risk.level.value, evidence, result.artifacts, execution=result)


def http_request(deps: ToolDependencies, request: HttpRequestInput) -> ToolObservation:
    """Make one authorized, challenge-scoped HTTP request through the configured executor."""

    started = time.monotonic()
    try:
        url = _with_params(request.url, request.params)
        _validate_authorized_url(deps, url)
    except ValueError as exc:
        return _record(deps, "http_request", started, False, "high", {"url": request.url}, error=str(exc))
    try:
        command = _curl_command(request, url)
    except ValueError as exc:
        return _record(deps, "http_request", started, False, "high", {"url": url}, error=str(exc))
    try:
        result = deps.context.executor.run(command, cwd=deps.context.layout.work_dir, timeout=request.timeout, env={})
    except (WorkspaceBoundaryError, RuntimeError, ValueError) as exc:
        return _record(deps, "http_request", started, False, "medium", {"url": url}, error=str(exc))
    deps.execution_batch.results.append(result)
    evidence = _execution_evidence(deps, result)
    evidence["request"] = {"method": request.method, "url": url, "parameter_names": sorted(request.params), "header_names": sorted(request.headers)}
    return _record(deps, "http_request", started, result.ok, "medium", evidence, result.artifacts, execution=result)


def inspect_binary(deps: ToolDependencies, request: InspectBinaryInput) -> ToolObservation:
    started = time.monotonic()
    try:
        path = _resolve_workspace_path(deps, request.path, must_exist=True)
        relative = _display_path(deps, path)
        command = " && ".join(
            [
                f"file -- {shlex.quote(relative)}",
                f"sha256sum -- {shlex.quote(relative)}",
                f"readelf -h -- {shlex.quote(relative)} 2>/dev/null || true",
            ]
        )
        result = deps.context.executor.run(command, cwd=deps.context.layout.work_dir, timeout=min(deps.context.timeout, 60), env={})
    except (OSError, WorkspaceBoundaryError, RuntimeError, ValueError) as exc:
        return _record(deps, "inspect_binary", started, False, "low", {"path": request.path}, error=str(exc))
    deps.execution_batch.results.append(result)
    evidence = _execution_evidence(deps, result)
    evidence["path"] = relative
    evidence["binary"] = _binary_evidence(result.stdout)
    return _record(deps, "inspect_binary", started, result.ok, "low", evidence, result.artifacts, execution=result)


def ask_verifier(deps: ToolDependencies, request: AskVerifierInput) -> ToolObservation:
    del request
    started = time.monotonic()
    previous = deps.context.metadata.get("execution_batch")
    deps.context.metadata["execution_batch"] = deps.execution_batch
    try:
        result = VerifierAgent().run(deps.context)
    except (RuntimeError, ValueError) as exc:
        return _record(deps, "ask_verifier", started, False, "low", {}, error=str(exc))
    finally:
        if previous is None:
            deps.context.metadata.pop("execution_batch", None)
        else:
            deps.context.metadata["execution_batch"] = previous
    evidence = {
        "candidate_count": len(result.candidates),
        "verified_count": sum(candidate.verified for candidate in result.candidates),
        "sources": sorted({candidate.source for candidate in result.candidates}),
        "solved_state_changed": False,
    }
    return _record(deps, "ask_verifier", started, True, "low", evidence)


def pause_for_human(deps: ToolDependencies, request: PauseForHumanInput) -> ToolObservation:
    started = time.monotonic()
    deps.context.state.metadata["pydantic_agent_pause_reason"] = request.reason
    deps.context.state.transition_to(ChallengeState.PAUSED)
    return _record(deps, "pause_for_human", started, True, "low", {"reason": request.reason, "paused": True})


def structured_tool_functions() -> dict[str, Any]:
    """Return the typed tool surface for later PydanticAI registration."""

    return {
        "read_file": read_file,
        "search_artifacts": search_artifacts,
        "run_command": run_command,
        "http_request": http_request,
        "inspect_binary": inspect_binary,
        "ask_verifier": ask_verifier,
        "pause_for_human": pause_for_human,
    }


def _resolve_workspace_path(deps: ToolDependencies, path: str, *, must_exist: bool) -> Path:
    _validate_solver_relative_path(path)
    resolved = resolve_inside(path, deps.context.layout.work_dir)
    if must_exist and (not resolved.exists() or not resolved.is_file()):
        raise ValueError(f"Path is not a readable file in this challenge workspace: {path}")
    return resolved


def _validate_solver_relative_path(path: str) -> None:
    raw = str(path).strip()
    candidate = Path(raw)
    if not raw or candidate.is_absolute() or PureWindowsPath(raw).is_absolute():
        raise ValueError(f"Path is not a challenge-relative file path: {path}")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"Path is outside workspace: unsafe segment in {path}")


def _display_path(deps: ToolDependencies, path: Path) -> str:
    return str(path.resolve().relative_to(deps.context.layout.work_dir.resolve()))


def _contains_network_command(command: str) -> bool:
    try:
        return any(Path(token).name.lower() in _NETWORK_COMMANDS for token in shlex.split(command))
    except ValueError:
        return True


def _network_authorized(deps: ToolDependencies, connection: str) -> bool:
    policy = local_executor_network_note(deps.context.config, deps.context.state.challenge)
    return bool(connection and policy.allowed)


def _validate_authorized_url(deps: ToolDependencies, url: str) -> None:
    connection = deps.context.state.challenge.connection or ""
    if not _network_authorized(deps, connection):
        raise ValueError("HTTP request denied: challenge network authorization is not enabled.")
    parsed = urlparse(url)
    expected = urlparse(connection if "://" in connection else f"//{connection}")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("HTTP request denied: only absolute http(s) URLs are supported.")
    if parsed.hostname != expected.hostname or _port(parsed) != _port(expected):
        raise ValueError("HTTP request denied: URL is outside the authorized challenge connection.")


def _port(parsed: Any) -> int | None:
    if parsed.port is not None:
        return parsed.port
    return {"http": 80, "https": 443}.get(parsed.scheme)


def _with_params(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = parsed.query
    extra = urlencode(params)
    return urlunparse(parsed._replace(query="&".join(part for part in (query, extra) if part)))


def _curl_command(request: HttpRequestInput, url: str) -> str:
    parts = ["curl", "--silent", "--show-error", "--include", "--request", shlex.quote(request.method), "--max-time", str(request.timeout)]
    for key, value in sorted(request.headers.items()):
        if "\n" in key or "\r" in key or "\n" in value or "\r" in value:
            raise ValueError("HTTP headers may not contain newlines")
        parts.extend(["--header", shlex.quote(f"{key}: {value}")])
    if request.body is not None:
        parts.extend(["--data-raw", shlex.quote(request.body)])
    parts.append(shlex.quote(url))
    return " ".join(parts)


def _execution_evidence(deps: ToolDependencies, result: ExecutionResult) -> dict[str, Any]:
    text = "\n".join(part for part in (result.stdout, result.stderr) if part)
    evidence = deps.observation_summarizer.summarize(text, timed_out=result.timed_out)
    evidence.update({"exit_code": result.exit_code, "timed_out": result.timed_out, "duration_seconds": result.duration_seconds})
    return evidence


def _record(
    deps: ToolDependencies,
    tool: str,
    started: float,
    ok: bool,
    risk: Literal["low", "medium", "high", "refuse"],
    observation: dict[str, Any],
    artifacts: list[Artifact] | None = None,
    *,
    error: str | None = None,
    execution: ExecutionResult | None = None,
) -> ToolObservation:
    duration = round(time.monotonic() - started, 6)
    artifact_list = list(artifacts or [])
    payload = ToolObservation(tool=tool, ok=ok, risk=risk, duration_seconds=duration, observation=observation, artifacts=[], error=error)
    evidence_artifact = _write_evidence_artifact(deps, payload)
    artifact_list.append(evidence_artifact)
    payload.artifacts = [artifact.to_dict() for artifact in artifact_list]
    step = Step(
        agent="pydantic-tools",
        action=tool,
        observations=[Observation(summary=f"{tool}: {'ok' if ok else 'rejected'}", raw=json.dumps(redact_value(observation), ensure_ascii=False, sort_keys=True), source=tool)],
        artifacts=artifact_list,
        exit_code=execution.exit_code if execution else (0 if ok else 1),
        started_at=execution.started_at if execution else utc_now(),
        ended_at=execution.ended_at if execution else utc_now(),
        metadata={"risk": risk, "duration_seconds": duration, "error": error},
    )
    if not deps.context.state.attempts or deps.context.state.attempts[-1].ended_at is not None:
        deps.context.state.start_attempt()
    deps.context.state.attempts[-1].add_step(step)
    deps.context.trace_store.append(
        TraceEvent(
            challenge_id=deps.context.state.challenge.id,
            agent="pydantic-tools",
            action=tool,
            command=["bash", "-lc", execution.command] if execution else None,
            stdout=json.dumps(redact_value(observation), ensure_ascii=False, sort_keys=True),
            stderr=error,
            artifacts=artifact_list,
            exit_code=step.exit_code,
            started_at=step.started_at,
            ended_at=step.ended_at,
            metadata={"risk": risk, "duration_seconds": duration, "ok": ok},
        )
    )
    return payload


def _write_evidence_artifact(deps: ToolDependencies, payload: ToolObservation) -> Artifact:
    output_dir = deps.context.layout.artifacts_dir / "pydantic-tools"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{int(time.time() * 1000)}-{payload.tool}.json"
    path.write_text(json.dumps(redact_value(payload.model_dump()), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return Artifact(path=str(path), kind="report", description=f"Structured {payload.tool} observation")


def _line_number(text: str, pattern: str) -> int:
    index = text.find(pattern)
    return text.count("\n", 0, index) + 1 if index >= 0 else 0


def _binary_evidence(output: str) -> dict[str, Any]:
    """Normalize existing file/readelf output without executing additional tools."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    file_type = lines[0].split(": ", 1)[-1] if lines else "unknown"
    machine = _binary_field(output, "Machine")
    arch_match = re.search(r"ELF\s+\d+-bit\s+\S+\s+([^,]+)", file_type)
    architecture = machine or (arch_match.group(1).strip() if arch_match else None)
    fmt = "ELF" if "ELF" in file_type else ("PE" if "PE32" in file_type else None)
    elf = {key.lower(): value for key in ("Class", "Data", "Type", "Machine", "Entry point address") if (value := _binary_field(output, key))}
    return {"file_type": file_type, "magic": file_type, "architecture": architecture, "format": fmt, "elf": elf if fmt == "ELF" else {}, "pe": {} if fmt != "PE" else {"detected": True}, "protections": {"available": False, "reason": "No checksec-style analyzer is registered for this profile."}, "tools": {name: shutil.which(name) is not None for name in ("file", "readelf", "sha256sum", "checksec")}}


def _binary_field(output: str, name: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(name)}:\s*(.+)$", output, re.MULTILINE)
    return match.group(1).strip() if match else None
