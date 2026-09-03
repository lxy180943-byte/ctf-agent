from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ctf_agent.core.config import get_nested
from ctf_agent.core.models import FlagCandidate, utc_now
from ctf_agent.core.state import ChallengeRunState, ChallengeState
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.platforms.base import SubmissionResult
from ctf_agent.platforms.ctfd import CTFdPlatformAdapter, adapter_from_config


@dataclass
class SubmitResult:
    challenge_id: str
    flag: str
    platform: str
    dry_run: bool
    submitted: bool
    accepted: bool | None
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "challenge_id": self.challenge_id,
            "flag": self.flag,
            "platform": self.platform,
            "dry_run": self.dry_run,
            "submitted": self.submitted,
            "accepted": self.accepted,
            "message": self.message,
            "metadata": dict(self.metadata),
        }


class Submitter:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def submit_run(self, run_dir: str | Path, *, flag: str | None = None, submit: bool = False) -> SubmitResult:
        run_path = Path(run_dir).expanduser().resolve()
        workspace_root = run_path.parent.parent if run_path.parent.name == "runs" else Path(get_nested(self.config, ("workspace_dir",)) or "~/ctf-workspace").expanduser()
        manager = WorkspaceManager(workspace_root)
        state = manager.load_state(run_path.name)
        selected = flag or self._select_flag(state)
        platform = str(state.challenge.metadata.get("source") or "local")

        if platform == "ctfd":
            result = self._submit_ctfd(state, selected, submit=submit)
        else:
            result = self._mark_local(state, selected, submit=submit)

        self._update_state(state, selected, result)
        manager.save_state(state)
        return result

    def _select_flag(self, state: ChallengeRunState) -> str:
        verified = [candidate for candidate in state.flag_candidates if candidate.verified]
        if not verified:
            raise ValueError("No verified flag candidate found; pass --flag explicitly to submit a chosen value")
        return sorted(verified, key=lambda item: (-item.confidence, item.value))[0].value

    def _mark_local(self, state: ChallengeRunState, flag: str, *, submit: bool) -> SubmitResult:
        if submit and state.state is not ChallengeState.SOLVED:
            state.state = ChallengeState.SOLVED
            state.updated_at = utc_now()
        return SubmitResult(
            challenge_id=state.challenge.id,
            flag=flag,
            platform="local",
            dry_run=not submit,
            submitted=bool(submit),
            accepted=True,
            message="local run marked solved" if submit else "dry-run: local run would be marked solved",
        )

    def _submit_ctfd(self, state: ChallengeRunState, flag: str, *, submit: bool) -> SubmitResult:
        profile = state.challenge.metadata.get("profile") or get_nested(self.config, ("platform", "ctfd", "default_profile"))
        try:
            adapter = adapter_from_config(self.config, str(profile) if profile else None)
        except Exception:
            url = get_nested(self.config, ("platform", "ctfd", "url"))
            token = get_nested(self.config, ("platform", "ctfd", "token"))
            if not url:
                return SubmitResult(
                    challenge_id=state.challenge.id,
                    flag=flag,
                    platform="ctfd",
                    dry_run=True,
                    submitted=False,
                    accepted=None,
                    message="dry-run: CTFd url is not configured",
                )
            adapter = CTFdPlatformAdapter(str(url), str(token or ""))
        adapter_result: SubmissionResult = adapter.submit_flag(state.challenge, flag, submit=submit)
        return SubmitResult(
            challenge_id=state.challenge.id,
            flag=flag,
            platform="ctfd",
            dry_run=not submit,
            submitted=adapter_result.submitted,
            accepted=adapter_result.accepted,
            message=adapter_result.message,
            metadata=adapter_result.metadata,
        )

    def _update_state(self, state: ChallengeRunState, flag: str, result: SubmitResult) -> None:
        matched = False
        for candidate in state.flag_candidates:
            if candidate.value == flag:
                candidate.submitted = result.submitted
                candidate.verified = candidate.verified or bool(result.accepted)
                matched = True
        if not matched:
            state.add_flag_candidate(
                FlagCandidate(
                    value=flag,
                    source="manual-submit",
                    confidence=0.5,
                    verified=bool(result.accepted),
                    submitted=result.submitted,
                )
            )
        state.metadata["last_submit"] = result.to_dict()
