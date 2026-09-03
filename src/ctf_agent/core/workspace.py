from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ctf_agent.core.models import Challenge
from ctf_agent.core.state import ChallengeRunState
from ctf_agent.core.trace import TraceEvent, TraceStore


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
    return slug or "challenge"


@dataclass
class WorkspaceLayout:
    root: Path
    challenge_dir: Path
    input_dir: Path
    work_dir: Path
    artifacts_dir: Path
    state_path: Path
    trace_path: Path


@dataclass
class ResumeData:
    state: ChallengeRunState
    trace_events: list[TraceEvent]
    layout: WorkspaceLayout


class WorkspaceManager:
    def __init__(self, workspace_root: str | Path = "~/ctf-workspace") -> None:
        self.workspace_root = Path(workspace_root).expanduser()
        self.runs_root = self.workspace_root / "runs"

    def layout_for(self, challenge_id: str) -> WorkspaceLayout:
        challenge_dir = self.runs_root / slugify(challenge_id)
        return WorkspaceLayout(
            root=self.workspace_root,
            challenge_dir=challenge_dir,
            input_dir=challenge_dir / "input",
            work_dir=challenge_dir / "work",
            artifacts_dir=challenge_dir / "artifacts",
            state_path=challenge_dir / "state.json",
            trace_path=challenge_dir / "trace.jsonl",
        )

    def prepare(self, challenge: Challenge) -> WorkspaceLayout:
        layout = self.layout_for(challenge.id)
        for directory in (layout.challenge_dir, layout.input_dir, layout.work_dir, layout.artifacts_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return layout

    def init_state(self, challenge: Challenge) -> ChallengeRunState:
        self.prepare(challenge)
        state = ChallengeRunState(challenge=challenge)
        self.save_state(state)
        return state

    def save_state(self, state: ChallengeRunState) -> Path:
        layout = self.prepare(state.challenge)
        tmp_path = layout.state_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(state.to_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(layout.state_path)
        return layout.state_path

    def load_state(self, challenge_id: str) -> ChallengeRunState:
        layout = self.layout_for(challenge_id)
        if not layout.state_path.exists():
            raise FileNotFoundError(f"No saved challenge state at {layout.state_path}")
        return ChallengeRunState.from_dict(json.loads(layout.state_path.read_text(encoding="utf-8")))

    def trace_store_for(self, challenge_id: str) -> TraceStore:
        return TraceStore(self.layout_for(challenge_id).trace_path)

    def resume(self, challenge_id: str) -> ResumeData:
        layout = self.layout_for(challenge_id)
        state = self.load_state(challenge_id)
        events = TraceStore(layout.trace_path).read_events()
        return ResumeData(state=state, trace_events=events, layout=layout)
