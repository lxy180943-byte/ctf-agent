from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ctf_agent.core.state import ChallengeRunState
from ctf_agent.core.trace import TraceEvent, TraceStore
from ctf_agent.core.workspace import WorkspaceManager


class Reporter:
    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self.workspace_root = Path(workspace_root).expanduser() if workspace_root else None

    def generate(self, run_dir: str | Path) -> Path:
        run_path = Path(run_dir).expanduser().resolve()
        workspace_root = run_path.parent.parent if run_path.parent.name == "runs" else self.workspace_root
        if workspace_root is None:
            workspace_root = run_path.parent.parent
        manager = WorkspaceManager(workspace_root)
        state = manager.load_state(run_path.name)
        events = TraceStore(run_path / "trace.jsonl").read_events()
        report_path = run_path / "writeup.md"
        report_path.write_text(self.render(state, events), encoding="utf-8")
        return report_path

    def render(self, state: ChallengeRunState, events: list[TraceEvent]) -> str:
        challenge = state.challenge
        commands = [event for event in events if event.agent == "executor" and event.action == "run-command"]
        failures = self._failure_messages(state, events)
        flags = [candidate.value for candidate in state.flag_candidates if candidate.verified]

        lines = [
            f"# {challenge.title}",
            "",
            "## Challenge",
            "",
            f"- id: `{challenge.id}`",
            f"- category: `{challenge.category}`",
            f"- state: `{state.state.value}`",
            f"- connection: `{challenge.connection or ''}`",
            "",
            "## Description",
            "",
            challenge.description or "_No description provided._",
            "",
            "## Files",
            "",
        ]
        lines.extend(f"- `{file_name}`" for file_name in challenge.files)
        if not challenge.files:
            lines.append("- _No files listed._")

        pipelines = self._specialist_pipelines(events)
        lines.extend(["", "## Specialist Triage", ""])
        if pipelines:
            for pipeline in pipelines:
                lines.extend(
                    [
                        f"### `{pipeline.get('category', 'unknown')}`",
                        "",
                        f"- hypothesis: {pipeline.get('hypothesis', '')}",
                    ]
                )
                evidence = pipeline.get("evidence", [])
                if evidence:
                    lines.append("- evidence:")
                    lines.extend(f"  - {item}" for item in evidence)
                next_commands = pipeline.get("next_commands", [])
                if next_commands:
                    lines.append("- next_commands:")
                    for command in next_commands[:12]:
                        if isinstance(command, dict):
                            lines.append(f"  - `{command.get('command', '')}`")
                notes = pipeline.get("notes", [])
                if notes:
                    lines.append("- notes:")
                    lines.extend(f"  - {item}" for item in notes)
                lines.append("")
        else:
            lines.append("_No specialist triage pipeline recorded._")

        lines.extend(["", "## Key Commands", ""])
        if commands:
            for event in commands:
                command = _format_command(event.command)
                lines.extend(
                    [
                        f"### `{command}`",
                        "",
                        f"- exit_code: `{event.exit_code}`",
                        f"- started_at: `{event.started_at}`",
                        f"- ended_at: `{event.ended_at}`",
                        "",
                        "stdout summary:",
                        "",
                        "```text",
                        _safe_text(event.stdout or ""),
                        "```",
                        "",
                    ]
                )
        else:
            lines.append("_No commands recorded._")

        lines.extend(["", "## Failed Routes", ""])
        if failures:
            lines.extend(f"- {failure}" for failure in failures)
        else:
            lines.append("- No failed route recorded.")

        lines.extend(["", "## Final Flag", ""])
        if flags:
            lines.extend(f"- `{flag}`" for flag in flags)
        else:
            lines.append("- _No verified flag candidate._")

        lines.extend(["", "## Reproduction Steps", ""])
        if commands:
            lines.append("```bash")
            lines.append("cd ~/ctf-agent")
            lines.append(f"ctf-agent inspect {challenge.metadata.get('source_dir', '<challenge_dir>')}")
            for event in commands:
                command = _format_command(event.command)
                if command:
                    lines.append(f"# {event.agent}: {event.action}")
                    lines.append(command)
            lines.append("```")
        else:
            lines.append("_No reproduction commands recorded._")

        lines.extend(
            [
                "",
                "## Raw Metadata",
                "",
                "```json",
                json.dumps({"challenge": challenge.to_dict(), "metadata": state.metadata}, indent=2, ensure_ascii=False, sort_keys=True),
                "```",
                "",
            ]
        )
        return "\n".join(lines)

    def _failure_messages(self, state: ChallengeRunState, events: list[TraceEvent]) -> list[str]:
        failures: list[str] = []
        if state.metadata.get("failure_count"):
            failures.append(f"failure_count={state.metadata['failure_count']}")
        for event in events:
            if "failure" in event.action or event.agent == "critic":
                detail = event.stdout or event.stderr or event.action
                failures.append(detail.strip())
            bus = event.metadata.get("message_bus") if isinstance(event.metadata, dict) else None
            if isinstance(bus, dict):
                for message in bus.get("messages", []):
                    if message.get("kind") == "failure_reason":
                        failures.append(str(message.get("content")))
        return list(dict.fromkeys(item for item in failures if item))


    def _specialist_pipelines(self, events: list[TraceEvent]) -> list[dict[str, Any]]:
        pipelines: list[dict[str, Any]] = []
        for event in events:
            if event.action != "specialist-triage" or not isinstance(event.metadata, dict):
                continue
            pipeline = event.metadata.get("pipeline")
            if isinstance(pipeline, dict):
                pipelines.append(pipeline)
        return pipelines


def _format_command(command: list[str] | None) -> str:
    if not command:
        return ""
    if len(command) >= 3 and command[0] in {"bash", "docker"} and command[-2] == "-lc":
        return command[-1]
    return " ".join(command)


def _safe_text(value: str) -> str:
    return "".join(ch if ch in "\n\r\t" or ord(ch) >= 32 else "\ufffd" for ch in value)
