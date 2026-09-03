from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from ctf_agent.core.models import Artifact, utc_now
from ctf_agent.core.trace import TraceEvent, TraceStore


class ExecutorError(RuntimeError):
    """Base executor error."""


class WorkspaceBoundaryError(ExecutorError):
    """Raised when a command attempts to run outside the configured workspace."""


class CommandSafetyError(ExecutorError):
    """Raised when a command violates the default destructive-operation policy."""


DESTRUCTIVE_COMMANDS = {
    "chmod",
    "chown",
    "dd",
    "mkfs",
    "mkswap",
    "mount",
    "mv",
    "rm",
    "rmdir",
    "shred",
    "sudo",
    "truncate",
    "umount",
    "unlink",
}

PATH_OPTION_ARGS = {
    "-o",
    "--backup",
    "--output",
    "--reference",
    "--target-directory",
}

DESTRUCTIVE_ARG_PREFIXES = ("of=", "if=", "conv=", "bs=", "count=", "skip=", "seek=", "status=")
SHELL_EXPANSION_MARKERS = ("$", "`", "<(", ">(", "{", "}")


@dataclass
class ExecutionResult:
    command: str
    cwd: str
    env: dict[str, str]
    timeout: int
    exit_code: int
    stdout: str
    stderr: str
    started_at: str
    ended_at: str
    duration_seconds: float
    timed_out: bool = False
    artifacts: list[Artifact] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Executor(ABC):
    @abstractmethod
    def run(self, command: str, cwd: str | Path, timeout: int | None = None, env: dict[str, str] | None = None) -> ExecutionResult:
        raise NotImplementedError


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    completed = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return completed.returncode == 0


def resolve_inside(path: str | Path, root: str | Path) -> Path:
    root_path = Path(root).expanduser().resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root_path / candidate
    resolved = candidate.resolve()
    if resolved != root_path and root_path not in resolved.parents:
        raise WorkspaceBoundaryError(f"Path is outside workspace: {resolved}")
    return resolved


def _looks_like_path(token: str) -> bool:
    return token.startswith(("/", "./", "../", "~")) or "/" in token


def _command_name(token: str) -> str:
    return Path(token).name


def validate_command_safety(command: str, cwd: Path, workspace_root: Path) -> None:
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise CommandSafetyError(f"Cannot parse command safely: {exc}") from exc
    if not tokens:
        raise CommandSafetyError("Refusing to run an empty command")

    workspace_root = workspace_root.resolve()
    destructive_seen = False
    skip_next = False
    checked_target = False
    for token in tokens:
        if token in {"&&", "||", ";", "|"}:
            destructive_seen = False
            skip_next = False
            checked_target = False
            continue
        if skip_next:
            _validate_destructive_target(skip_next, token, cwd, workspace_root)
            checked_target = True
            skip_next = False
            continue
        command_name = _command_name(token)
        if command_name in DESTRUCTIVE_COMMANDS:
            if command_name == "sudo":
                raise CommandSafetyError("Refusing privileged command: sudo")
            destructive_seen = True
            checked_target = False
            continue
        if not destructive_seen:
            continue
        if token in PATH_OPTION_ARGS:
            skip_next = token
            continue
        if token.startswith("-"):
            continue
        target = token
        if "=" in token:
            key, value = token.split("=", 1)
            if key not in {"of", "if", "file", "dest", "target"}:
                continue
            target = value
        if not target or target.isdigit():
            continue
        _validate_destructive_target(command_name, target, cwd, workspace_root)
        checked_target = True

    if destructive_seen and not checked_target:
        raise CommandSafetyError("Refusing destructive command without an explicit workspace-scoped target")


def _validate_destructive_target(context: str, token: str, cwd: Path, workspace_root: Path) -> None:
    if any(marker in token for marker in SHELL_EXPANSION_MARKERS):
        raise CommandSafetyError(f"Refusing destructive operation with shell-expanded target: {token}")
    candidate = Path(token).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    resolved = candidate.resolve(strict=False)
    if resolved == workspace_root:
        raise CommandSafetyError("Refusing destructive operation targeting the workspace root")
    if workspace_root not in resolved.parents:
        raise CommandSafetyError(f"Refusing destructive operation outside workspace: {token}")


class LocalExecutor(Executor):
    def __init__(self, workspace_root: str | Path, trace_store: TraceStore | None = None, challenge_id: str | None = None) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.trace_store = trace_store
        self.challenge_id = challenge_id or "unknown"

    def run(self, command: str, cwd: str | Path, timeout: int | None = None, env: dict[str, str] | None = None) -> ExecutionResult:
        timeout = int(timeout or 60)
        cwd_path = resolve_inside(cwd, self.workspace_root)
        cwd_path.mkdir(parents=True, exist_ok=True)
        validate_command_safety(command, cwd_path, self.workspace_root)
        started_at = utc_now()
        started = time.monotonic()
        merged_env = {**os.environ, **(env or {})}
        exit_code = 0
        stdout = ""
        stderr = ""
        timed_out = False

        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd_path),
                env=merged_env,
                shell=True,
                executable="/bin/bash",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
            stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
            stderr += f"\nCommand timed out after {timeout}s"
            timed_out = True

        ended_at = utc_now()
        result = ExecutionResult(
            command=command,
            cwd=str(cwd_path),
            env=dict(env or {}),
            timeout=timeout,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=round(time.monotonic() - started, 6),
            timed_out=timed_out,
            metadata={"executor": "local"},
        )
        self._write_result_trace(result)
        return result

    def _write_result_trace(self, result: ExecutionResult) -> None:
        if not self.trace_store:
            return
        artifacts = write_output_artifacts(
            self.trace_store.path.parent / "artifacts" / "command-output",
            result,
        )
        result.artifacts.extend(artifacts)
        self.trace_store.append(
            TraceEvent(
                challenge_id=self.challenge_id,
                agent="executor",
                action="run-command",
                command=["bash", "-lc", result.command],
                stdout=result.stdout,
                stderr=result.stderr,
                artifacts=result.artifacts,
                exit_code=result.exit_code,
                started_at=result.started_at,
                ended_at=result.ended_at,
                metadata={
                    "executor": "local",
                    "cwd": result.cwd,
                    "env": result.env,
                    "timeout": result.timeout,
                    "timed_out": result.timed_out,
                    "duration_seconds": result.duration_seconds,
                },
            )
        )


def write_output_artifacts(output_dir: str | Path, result: ExecutionResult) -> list[Artifact]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    prefix = f"{int(time.time())}-{uuid4().hex[:8]}"
    stdout_path = output_path / f"{prefix}.stdout.txt"
    stderr_path = output_path / f"{prefix}.stderr.txt"
    stdout_path.write_text(result.stdout, encoding="utf-8", errors="replace")
    stderr_path.write_text(result.stderr, encoding="utf-8", errors="replace")
    return [
        Artifact(path=str(stdout_path), kind="stdout", description=f"Full stdout for command: {result.command}"),
        Artifact(path=str(stderr_path), kind="stderr", description=f"Full stderr for command: {result.command}"),
    ]
