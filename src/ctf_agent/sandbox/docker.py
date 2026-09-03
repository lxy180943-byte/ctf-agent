from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

from ctf_agent.core.models import utc_now
from ctf_agent.core.trace import TraceEvent, TraceStore
from ctf_agent.sandbox.executor import (
    ExecutionResult,
    Executor,
    WorkspaceBoundaryError,
    docker_available,
    resolve_inside,
    validate_command_safety,
    write_output_artifacts,
)


class DockerExecutor(Executor):
    def __init__(
        self,
        workspace_root: str | Path,
        image: str,
        *,
        network: str = "none",
        memory: str | None = None,
        cpu: float | str | None = None,
        trace_store: TraceStore | None = None,
        challenge_id: str | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.image = image
        self.network = network
        self.memory = memory
        self.cpu = str(cpu) if cpu is not None else None
        self.trace_store = trace_store
        self.challenge_id = challenge_id or "unknown"

    def run(self, command: str, cwd: str | Path, timeout: int | None = None, env: dict[str, str] | None = None) -> ExecutionResult:
        timeout = int(timeout or 60)
        if not docker_available():
            raise RuntimeError("Docker is not available")
        cwd_path = resolve_inside(cwd, self.workspace_root)
        cwd_path.mkdir(parents=True, exist_ok=True)
        validate_command_safety(command, cwd_path, self.workspace_root)
        try:
            relative_cwd = cwd_path.relative_to(self.workspace_root)
        except ValueError as exc:
            raise WorkspaceBoundaryError(f"CWD is outside workspace: {cwd_path}") from exc
        container_cwd = Path("/workspace") / relative_cwd
        docker_cmd = self._docker_command(command, container_cwd, timeout, env or {})

        started_at = utc_now()
        started = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(
                docker_cmd,
                cwd=str(self.workspace_root),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout + 5,
                check=False,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
            stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
            stderr += f"\nDocker command timed out after {timeout}s"
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
            metadata={
                "executor": "docker",
                "image": self.image,
                "network": self.network,
                "docker_command": docker_cmd,
            },
        )
        self._write_result_trace(result)
        return result

    def _docker_command(self, command: str, container_cwd: Path, timeout: int, env: dict[str, str]) -> list[str]:
        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--network",
            self.network,
            "--workdir",
            str(container_cwd),
            "-v",
            f"{self.workspace_root}:/workspace",
        ]
        if self.memory:
            docker_cmd.extend(["--memory", self.memory])
        if self.cpu:
            docker_cmd.extend(["--cpus", self.cpu])
        for key, value in sorted(env.items()):
            docker_cmd.extend(["-e", f"{key}={value}"])
        docker_cmd.extend([self.image, "bash", "-lc", f"timeout {timeout}s {command}"])
        return docker_cmd

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
                command=["docker", self.image, "bash", "-lc", result.command],
                stdout=result.stdout,
                stderr=result.stderr,
                artifacts=result.artifacts,
                exit_code=result.exit_code,
                started_at=result.started_at,
                ended_at=result.ended_at,
                metadata={
                    "executor": "docker",
                    "image": self.image,
                    "network": self.network,
                    "cwd": result.cwd,
                    "env": result.env,
                    "timeout": result.timeout,
                    "timed_out": result.timed_out,
                    "duration_seconds": result.duration_seconds,
                },
            )
        )


def image_for_category(config: dict, category: str | None = None) -> str:
    sandbox = config.get("sandbox", {})
    images = sandbox.get("images", {})
    profile = category or sandbox.get("default_profile") or "generic"
    return images.get(profile) or images.get("generic") or sandbox.get("image") or "ctf-agent:generic"
