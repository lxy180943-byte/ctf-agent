from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ctf_agent.core.state import ChallengeRunState, ChallengeState
from ctf_agent.core.trace import TraceEvent, TraceStore, summarize_text
from ctf_agent.core.workspace import WorkspaceManager


@dataclass
class RunReview:
    run_dir: Path
    challenge_id: str
    title: str
    category: str
    state: str
    key_hypotheses: list[str] = field(default_factory=list)
    effective_commands: list[str] = field(default_factory=list)
    ineffective_commands: list[str] = field(default_factory=list)
    missed_signals: list[str] = field(default_factory=list)
    next_strategies: list[str] = field(default_factory=list)
    solved_flags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_dir": str(self.run_dir),
            "challenge_id": self.challenge_id,
            "title": self.title,
            "category": self.category,
            "state": self.state,
            "key_hypotheses": list(self.key_hypotheses),
            "effective_commands": list(self.effective_commands),
            "ineffective_commands": list(self.ineffective_commands),
            "missed_signals": list(self.missed_signals),
            "next_strategies": list(self.next_strategies),
            "solved_flags": list(self.solved_flags),
            "metadata": dict(self.metadata),
        }

    def render_markdown(self) -> str:
        lines = [
            f"# Run Review: {self.challenge_id}",
            "",
            f"- title: `{self.title}`",
            f"- category: `{self.category}`",
            f"- state: `{self.state}`",
            f"- run_dir: `{self.run_dir}`",
            "",
            "## Key Hypotheses",
            "",
            *_bullets(self.key_hypotheses, "No explicit hypotheses recorded."),
            "",
            "## Effective Commands",
            "",
            *_bullets(self.effective_commands, "No effective commands recorded."),
            "",
            "## Ineffective Commands",
            "",
            *_bullets(self.ineffective_commands, "No ineffective commands recorded."),
            "",
            "## Missed Signals",
            "",
            *_bullets(self.missed_signals, "No missed signals detected from the trace."),
            "",
            "## Next Strategies",
            "",
            *_bullets(self.next_strategies, "No next strategy generated."),
        ]
        if self.solved_flags:
            lines.extend(["", "## Verified Flags", "", *_bullets(self.solved_flags, "")])
        return "\n".join(lines) + "\n"


class RunReviewer:
    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self.workspace_root = Path(workspace_root).expanduser() if workspace_root else None

    def review(self, run_dir: str | Path, *, write: bool = True) -> RunReview:
        run_path = Path(run_dir).expanduser().resolve()
        if not run_path.is_dir():
            raise FileNotFoundError(f"Run directory not found: {run_path}")
        workspace_root = run_path.parent.parent if run_path.parent.name == "runs" else self.workspace_root or run_path.parent.parent
        manager = WorkspaceManager(workspace_root)
        state = manager.load_state(run_path.name)
        events = TraceStore(run_path / "trace.jsonl").read_events()
        review = build_run_review(state, events, run_path)
        if write:
            (run_path / "run_review.md").write_text(review.render_markdown(), encoding="utf-8")
        return review


def build_run_review(state: ChallengeRunState, events: list[TraceEvent], run_dir: Path) -> RunReview:
    flags = [candidate.value for candidate in state.flag_candidates if candidate.verified]
    effective = _effective_commands(events)
    ineffective = _ineffective_commands(events)
    hypotheses = _hypotheses(events, state)
    missed = _missed_signals(state, events, ineffective)
    next_strategies = _next_strategies(state, effective, ineffective, missed)
    return RunReview(
        run_dir=run_dir,
        challenge_id=state.challenge.id,
        title=state.challenge.title,
        category=state.challenge.category,
        state=state.state.value,
        key_hypotheses=hypotheses,
        effective_commands=effective,
        ineffective_commands=ineffective,
        missed_signals=missed,
        next_strategies=next_strategies,
        solved_flags=flags,
        metadata={
            "attempt_count": len(state.attempts),
            "failure_count": state.metadata.get("failure_count", 0),
            "event_count": len(events),
        },
    )


def _hypotheses(events: list[TraceEvent], state: ChallengeRunState) -> list[str]:
    values: list[str] = []
    classification = state.metadata.get("classification")
    if isinstance(classification, dict) and classification.get("category"):
        values.append(f"classified as {classification['category']}")
    for event in events:
        text = ""
        if event.action in {"classify", "plan", "specialist-triage", "decision"}:
            text = event.stdout or ""
        pipeline = event.metadata.get("pipeline") if isinstance(event.metadata, dict) else None
        if isinstance(pipeline, dict) and pipeline.get("hypothesis"):
            text = str(pipeline["hypothesis"])
        if text:
            _append_unique(values, summarize_text(text.strip(), 500) or text.strip())
    return values[:12]


def _effective_commands(events: list[TraceEvent]) -> list[str]:
    commands: list[str] = []
    for event in events:
        if event.action != "run-command" or event.exit_code != 0:
            continue
        text = (event.stdout or "") + "\n" + (event.stderr or "")
        if text.strip() or event.artifacts:
            _append_unique(commands, _format_command(event.command))
    return [command for command in commands if command][:20]


def _ineffective_commands(events: list[TraceEvent]) -> list[str]:
    commands: list[str] = []
    for event in events:
        if event.action != "run-command":
            continue
        timed_out = bool(event.metadata.get("timed_out")) if isinstance(event.metadata, dict) else False
        if event.exit_code not in (None, 0) or timed_out:
            command = _format_command(event.command)
            reason = f"{command}  # exit={event.exit_code}"
            if timed_out:
                reason += " timeout=true"
            detail = summarize_text((event.stderr or event.stdout or "").strip(), 180)
            if detail:
                reason += f" | {detail}"
            _append_unique(commands, reason)
    return commands[:20]


def _missed_signals(state: ChallengeRunState, events: list[TraceEvent], ineffective: list[str]) -> list[str]:
    missed: list[str] = []
    if state.state is not ChallengeState.SOLVED and state.flag_candidates:
        missed.append(f"{len(state.flag_candidates)} flag candidate(s) were found but not verified/submitted.")
    if state.state is not ChallengeState.SOLVED and any("flag{" in (event.stdout or "") for event in events):
        missed.append("Trace output contained a common flag-looking token, but the run did not end solved.")
    if ineffective:
        missed.append("One or more commands failed; tool availability, cwd, or file names may have been assumed too early.")
    if not any(event.action == "search-artifacts" for event in events) and state.state is not ChallengeState.SOLVED:
        missed.append("No artifact search step was recorded before the run stopped.")
    if state.challenge.files and not any("file " in _format_command(event.command) for event in events if event.command):
        missed.append("Attached files were not explicitly typed with `file`; category routing may have missed file magic.")
    return missed[:10]


def _next_strategies(state: ChallengeRunState, effective: list[str], ineffective: list[str], missed: list[str]) -> list[str]:
    strategies: list[str] = []
    if state.state is ChallengeState.SOLVED:
        strategies.append("Promote the successful route after confirming it reproduces from a clean workspace.")
    else:
        strategies.append("Start the next attempt with three non-destructive triage commands, then verify observations before branching.")
    if ineffective:
        strategies.append("Demote or annotate failed commands so planner memory does not retry them blindly.")
    if missed:
        strategies.append("Convert missed signals into explicit verifier or specialist checks.")
    category = (state.challenge.category or "misc").lower()
    strategies.append(_category_strategy(category))
    return strategies


def _category_strategy(category: str) -> str:
    return {
        "pwn": "For pwn, collect file/checksec/readelf/strings before drafting exploit assumptions.",
        "rev": "For rev, identify format and strings first, then choose objdump/r2/decompiler routes.",
        "crypto": "For crypto, classify encoding/RSA/PRNG/substitution symptoms before writing solve.py.",
        "web": "For web, capture headers/body/robots/forms and keep fuzzing scoped to the challenge connection.",
        "forensics": "For forensics, run file/exiftool/binwalk/strings and inspect carved outputs.",
    }.get(category, "For misc, classify artifact type first and keep the first loop small.")


def _format_command(command: list[str] | None) -> str:
    if not command:
        return ""
    if len(command) >= 3 and command[0] in {"bash", "docker"} and command[-2] == "-lc":
        return command[-1]
    return " ".join(command)


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _bullets(values: list[str], empty: str) -> list[str]:
    if not values:
        return [f"- {empty}"] if empty else []
    return [f"- {value}" for value in values]
