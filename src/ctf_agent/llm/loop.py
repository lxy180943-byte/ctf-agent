from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ctf_agent.agents.base import AgentContext
from ctf_agent.analysis.php import summarize_php_observation
from ctf_agent.analysis.observation import ObservationSummarizer
from ctf_agent.agents.executor import ExecutionBatch
from ctf_agent.agents.verifier import VerificationResult, VerifierAgent
from ctf_agent.core.config import get_nested
from ctf_agent.core.flag_detector import FlagDetector
from ctf_agent.core.models import Artifact, FlagCandidate, Observation, Step, utc_now
from ctf_agent.core.state import ChallengeRunState, ChallengeState
from ctf_agent.core.trace import TraceEvent, summarize_text
from ctf_agent.llm.actions import ActionGuardError, ActionType, LLMAction, parse_action_decision
from ctf_agent.llm.provider import LLMMessage
from ctf_agent.llm.risk import RiskLevel, classify_command_risk
from ctf_agent.sandbox import ExecutionResult, WorkspaceBoundaryError

_OBSERVATION_LIMIT = 1800
_FILE_READ_LIMIT = 200_000
_SEARCH_FILE_LIMIT = 250_000


@dataclass
class ObservationRecord:
    summary: str
    source: str
    raw: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "summary": self.summary,
            "source": self.source,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }
        if self.raw is not None:
            data["raw"] = summarize_text(self.raw, _OBSERVATION_LIMIT)
        return data


@dataclass
class LLMActionLoopResult:
    solved: bool
    paused: bool = False
    steps_executed: int = 0
    batch: ExecutionBatch = field(default_factory=ExecutionBatch)
    verification: VerificationResult = field(default_factory=VerificationResult)
    observations: list[ObservationRecord] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


class LLMActionLoop:
    def __init__(self, *, verifier: VerifierAgent | None = None, save_state: Callable[[ChallengeRunState], Path] | None = None) -> None:
        self.verifier = verifier or VerifierAgent()
        self.save_state = save_state
        self.observation_summarizer = ObservationSummarizer()

    def run(self, context: AgentContext) -> LLMActionLoopResult:
        if context.llm_provider is None or context.prompt_store is None:
            raise ValueError("LLMActionLoop requires llm_provider and prompt_store")
        state = context.state
        if not state.attempts or state.attempts[-1].ended_at is not None:
            state.start_attempt()
        self._transition(state, ChallengeState.ANALYZING)
        self._save(state)

        result = LLMActionLoopResult(solved=False, metadata={"provider": context.llm_provider.name})
        observed_paths = self._initial_observed_paths(context)
        observed_texts = self._initial_observed_texts(context)

        for round_index in range(1, context.max_steps + 1):
            try:
                response = context.llm_provider.complete(
                    messages=[
                        LLMMessage(role="system", content="Return strict JSON tool actions for an authorized local CTF agent. Never invent observations."),
                        LLMMessage(role="user", content=self._render_prompt(context, result.observations, observed_paths)),
                    ],
                    response_format="json_object",
                    max_tokens=int(get_nested(context.config, ("llm", "max_tokens")) or 1200),
                )
                decision = parse_action_decision(response.content, max_actions=3)
            except Exception as exc:
                observation = ObservationRecord(
                    summary=f"LLM action response rejected: {exc}",
                    source="llm-guard",
                    metadata={"round": round_index, "kind": "invalid_json_or_schema"},
                )
                result.observations.append(observation)
                self._trace(context, "action-validation-failed", stderr=str(exc), metadata=observation.metadata)
                result.steps_executed += 1
                continue

            self._trace(context, "decision", stdout=decision.rationale, metadata={"round": round_index, "decision": decision.to_dict()})
            for action in decision.actions:
                if result.steps_executed >= context.max_steps:
                    break
                observations = self._execute_action(context, action, result.batch, observed_paths, observed_texts, round_index)
                result.observations.extend(observations)
                result.steps_executed += 1
                verification = context.metadata.get("llm_loop_verification")
                if isinstance(verification, VerificationResult):
                    result.verification = verification
                    if verification.solved:
                        return self._complete(context, result, ChallengeState.SOLVED)
                if context.state.state is ChallengeState.PAUSED:
                    result.paused = True
                    self._finish_attempt(state)
                    self._save(state)
                    return result
                if observations and observations[-1].metadata.get("finished") is True:
                    result.solved = any(candidate.verified for candidate in context.state.flag_candidates)
                    return self._complete(context, result, ChallengeState.SOLVED if result.solved else ChallengeState.FAILED)

        result.verification = self._run_verifier(context, result.batch)
        result.solved = result.verification.solved
        return self._complete(context, result, ChallengeState.SOLVED if result.solved else ChallengeState.FAILED)

    def _execute_action(
        self,
        context: AgentContext,
        action: LLMAction,
        batch: ExecutionBatch,
        observed_paths: set[str],
        observed_texts: list[str],
        round_index: int,
    ) -> list[ObservationRecord]:
        try:
            self._guard_action(context, action, observed_paths, observed_texts)
        except ActionGuardError as exc:
            observation = ObservationRecord(
                summary=f"Action rejected by hallucination guard: {exc}",
                source="llm-guard",
                metadata={"action": action.to_dict(), "round": round_index},
            )
            self._trace(context, "guard-reject", stderr=str(exc), metadata=observation.metadata)
            return [observation]
        if action.type is ActionType.RUN_COMMAND:
            return [self._run_command(context, action, batch, observed_paths, observed_texts, round_index)]
        if action.type is ActionType.READ_FILE:
            return [self._read_file(context, action, batch, observed_paths, observed_texts, round_index)]
        if action.type is ActionType.WRITE_FILE:
            return [self._write_file(context, action, observed_paths, round_index)]
        if action.type is ActionType.SEARCH_ARTIFACTS:
            return [self._search_artifacts(context, action, observed_paths, observed_texts, round_index)]
        if action.type is ActionType.ASK_VERIFIER:
            verification = self._run_verifier(context, batch)
            context.metadata["llm_loop_verification"] = verification
            return [
                ObservationRecord(
                    summary=f"Verifier found {len(verification.candidates)} candidate(s); solved={verification.solved}",
                    source="verifier",
                    raw="\n".join(candidate.value for candidate in verification.candidates),
                    metadata={"candidate_count": len(verification.candidates), "solved": verification.solved, "round": round_index},
                )
            ]
        if action.type is ActionType.FINISH:
            return [self._finish(context, action, observed_texts, round_index)]
        if action.type is ActionType.PAUSE:
            context.state.transition_to(ChallengeState.PAUSED)
            self._save(context.state)
            self._trace(context, "pause", stdout=action.reason, metadata={"round": round_index, "action": action.to_dict()})
            return [ObservationRecord(summary="Run paused by LLM action", source="orchestrator", metadata={"round": round_index})]
        raise ActionGuardError(f"Unsupported action type: {action.type}")

    def _run_command(self, context: AgentContext, action: LLMAction, batch: ExecutionBatch, observed_paths: set[str], observed_texts: list[str], round_index: int) -> ObservationRecord:
        command = action.command or ""
        risk = classify_command_risk(command, context.state.challenge.connection)
        if risk.level is RiskLevel.REFUSE or risk.confirm_required:
            self._trace(
                context,
                "command-risk-blocked",
                command=["bash", "-lc", command],
                stderr=risk.reason,
                metadata={"risk": risk.to_dict(), "action": action.to_dict(), "round": round_index},
            )
            return ObservationRecord(
                summary=f"Command not executed: {risk.reason}",
                source="risk-classifier",
                metadata={"risk": risk.to_dict(), "confirm_required": risk.confirm_required, "round": round_index},
            )
        self._transition(context.state, ChallengeState.RUNNING)
        self._save(context.state)
        try:
            execution = context.executor.run(command, cwd=context.layout.work_dir, timeout=action.timeout or context.timeout, env={})
        except (WorkspaceBoundaryError, RuntimeError, ValueError) as exc:
            self._trace(context, "command-failed-before-run", command=["bash", "-lc", command], stderr=str(exc), metadata={"round": round_index})
            return ObservationRecord(summary=f"Command failed before execution: {exc}", source="executor", metadata={"round": round_index})
        batch.results.append(execution)
        text = "\n".join(part for part in [execution.stdout, execution.stderr] if part)
        if text:
            observed_texts.append(text)
        structured_evidence = self.observation_summarizer.summarize(text)
        structured_evidence = self.observation_summarizer.summarize(text, timed_out=execution.timed_out)
        php_analysis = self._record_php_analysis(context, text, source="executor", observed_texts=observed_texts, round_index=round_index)
        self._discover_paths(context, observed_paths)
        step_observations = [Observation(summary=summarize_text(text, 500) or "", raw=summarize_text(text, 4000), source="executor")]
        if php_analysis:
            step_observations.append(Observation(summary="Structured PHP source analysis", raw=json.dumps(php_analysis, ensure_ascii=False, sort_keys=True), source="php-analyzer"))
        context.state.attempts[-1].add_step(
            Step(
                agent="llm-action-loop",
                action=action.reason or "LLM run_command",
                command=["bash", "-lc", command],
                observations=step_observations,
                artifacts=execution.artifacts,
                exit_code=execution.exit_code,
                started_at=execution.started_at,
                ended_at=execution.ended_at,
                metadata={"timeout": execution.timeout, "timed_out": execution.timed_out, "cwd": execution.cwd, "risk": risk.to_dict(), "php_analysis": php_analysis, "structured_evidence": structured_evidence},
            )
        )
        self._save(context.state)
        return ObservationRecord(
            summary=f"Command exit={execution.exit_code}, timed_out={execution.timed_out}: {summarize_text(text, 700) or '<no output>'}",
            source="executor",
            raw=text,
            metadata={"command": command, "exit_code": execution.exit_code, "risk": risk.to_dict(), "round": round_index, "php_analysis": php_analysis, "structured_evidence": structured_evidence},
        )

    def _read_file(self, context: AgentContext, action: LLMAction, batch: ExecutionBatch, observed_paths: set[str], observed_texts: list[str], round_index: int) -> ObservationRecord:
        path = self._resolve_existing_visible_path(context, action.path or "")
        data = path.read_bytes()[:_FILE_READ_LIMIT]
        text = data.decode("utf-8", errors="replace")
        rel = self._display_path(context, path)
        observed_paths.add(rel)
        observed_texts.append(text)
        structured_evidence = self.observation_summarizer.summarize(text)
        php_analysis = self._record_php_analysis(context, text, source=rel, observed_texts=observed_texts, round_index=round_index)
        batch.results.append(
            ExecutionResult(
                command=f"read_file {rel}",
                cwd=str(context.layout.work_dir),
                env={},
                timeout=0,
                exit_code=0,
                stdout=text,
                stderr="",
                started_at=utc_now(),
                ended_at=utc_now(),
                duration_seconds=0.0,
                metadata={"executor": "llm-action-loop", "action": "read_file"},
            )
        )
        artifact = Artifact(path=str(path), kind="text", description=f"Read by LLM action loop: {rel}")
        context.state.attempts[-1].add_step(
            Step(
                agent="llm-action-loop",
                action=action.reason or "LLM read_file",
                observations=[Observation(summary=summarize_text(text, 500) or "", raw=summarize_text(text, 4000), source=rel)],
                artifacts=[artifact],
                exit_code=0,
                metadata={"path": rel, "php_analysis": php_analysis, "structured_evidence": structured_evidence},
            )
        )
        self._trace(context, "read-file", stdout=text, artifacts=[artifact], metadata={"path": rel, "round": round_index, "php_analysis": php_analysis, "structured_evidence": structured_evidence})
        self._save(context.state)
        return ObservationRecord(summary=f"Read {rel}: {summarize_text(text, 700)}", source=rel, raw=text, metadata={"path": rel, "round": round_index, "php_analysis": php_analysis, "structured_evidence": structured_evidence})

    def _write_file(self, context: AgentContext, action: LLMAction, observed_paths: set[str], round_index: int) -> ObservationRecord:
        path = self._resolve_writable_visible_path(context, action.path or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(action.content or "", encoding="utf-8")
        rel = self._display_path(context, path)
        observed_paths.add(rel)
        artifact = Artifact(path=str(path), kind="text", description=f"Written by LLM action loop: {rel}")
        context.state.attempts[-1].add_step(
            Step(
                agent="llm-action-loop",
                action=action.reason or "LLM write_file",
                artifacts=[artifact],
                exit_code=0,
                metadata={"path": rel, "bytes": len((action.content or "").encode("utf-8"))},
            )
        )
        self._trace(context, "write-file", artifacts=[artifact], metadata={"path": rel, "round": round_index})
        self._save(context.state)
        return ObservationRecord(summary=f"Wrote {rel}", source="write_file", metadata={"path": rel, "round": round_index})

    def _search_artifacts(self, context: AgentContext, action: LLMAction, observed_paths: set[str], observed_texts: list[str], round_index: int) -> ObservationRecord:
        pattern = action.pattern or ""
        matches: list[dict[str, object]] = []
        for root in (context.layout.work_dir, context.layout.artifacts_dir):
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or len(matches) >= 40:
                    continue
                try:
                    text = path.read_bytes()[:_SEARCH_FILE_LIMIT].decode("utf-8", errors="replace")
                except OSError:
                    continue
                if pattern in text:
                    rel = self._display_path(context, path)
                    observed_paths.add(rel)
                    matches.append({"path": rel, "line": _line_number(text, pattern)})
                    observed_texts.append(pattern)
        raw = json.dumps(matches, ensure_ascii=False)
        self._trace(context, "search-artifacts", stdout=raw, metadata={"pattern": pattern, "match_count": len(matches), "round": round_index})
        return ObservationRecord(
            summary=f"search_artifacts pattern={pattern!r} matched {len(matches)} file(s)",
            source="search_artifacts",
            raw=raw,
            metadata={"pattern": pattern, "match_count": len(matches), "round": round_index},
        )

    def _finish(self, context: AgentContext, action: LLMAction, observed_texts: list[str], round_index: int) -> ObservationRecord:
        flag = action.flag
        if flag:
            detector = FlagDetector(context.state.challenge.flag_regex)
            candidates = detector.detect_text(flag, source="llm-finish")
            verified = any(candidate.value == flag and candidate.verified for candidate in candidates)
            if not verified:
                verified = any(candidate.value == flag and candidate.verified for candidate in context.state.flag_candidates)
            if verified:
                if not any(candidate.value == flag for candidate in context.state.flag_candidates):
                    context.state.add_flag_candidate(FlagCandidate(value=flag, source="llm-finish", confidence=0.95, verified=True))
                self._trace(context, "finish", stdout=flag, metadata={"round": round_index, "solved": True})
                self._save(context.state)
                return ObservationRecord(summary=f"Finished with verified flag {flag}", source="finish", raw=flag, metadata={"finished": True, "round": round_index})
        self._trace(context, "finish", stdout=flag or "", metadata={"round": round_index, "solved": False})
        return ObservationRecord(summary="Finished without a verified flag", source="finish", raw=flag, metadata={"finished": True, "round": round_index})

    def _run_verifier(self, context: AgentContext, batch: ExecutionBatch) -> VerificationResult:
        self._transition(context.state, ChallengeState.VERIFYING)
        self._save(context.state)
        previous = context.metadata.get("execution_batch")
        context.metadata["execution_batch"] = batch
        try:
            verification = self.verifier.run(context)
        finally:
            if previous is not None:
                context.metadata["execution_batch"] = previous
        for candidate in verification.candidates:
            if context.message_bus is not None:
                context.message_bus.add_flag_candidate("verifier", candidate)
        self._save(context.state)
        return verification

    def _guard_action(self, context: AgentContext, action: LLMAction, observed_paths: set[str], observed_texts: list[str]) -> None:
        if action.type is ActionType.READ_FILE:
            self._resolve_existing_visible_path(context, action.path or "")
        elif action.type is ActionType.WRITE_FILE:
            self._resolve_writable_visible_path(context, action.path or "")
        elif action.type is ActionType.RUN_COMMAND:
            self._guard_command_paths(context, action.command or "", observed_paths)
        elif action.type is ActionType.FINISH and action.flag:
            observed = "\n".join(observed_texts + [candidate.value for candidate in context.state.flag_candidates])
            if action.flag not in observed:
                raise ActionGuardError(f"finish flag was not present in prior observations: {action.flag}")
        elif action.type is ActionType.SEARCH_ARTIFACTS and not (action.pattern or "").strip():
            raise ActionGuardError("search_artifacts requires a non-empty pattern")

    def _guard_command_paths(self, context: AgentContext, command: str, observed_paths: set[str]) -> None:
        try:
            tokens = shlex.split(command)
        except ValueError as exc:
            raise ActionGuardError(f"Cannot parse command for path guard: {exc}") from exc
        for token in tokens[1:]:
            if token.startswith("-") or "://" in token or token in {"|", "&&", "||", ";"}:
                continue
            if not _is_explicit_path_token(token):
                continue
            path = self._resolve_existing_visible_path(context, token)
            rel = self._display_path(context, path)
            if rel not in observed_paths:
                raise ActionGuardError(f"Command references an unobserved path: {token}")

    def _resolve_existing_visible_path(self, context: AgentContext, value: str) -> Path:
        if not value or "\x00" in value:
            raise ActionGuardError("Path must be a non-empty relative path")
        raw = Path(value).expanduser()
        if raw.is_absolute():
            raise ActionGuardError(f"Absolute paths are not allowed: {value}")
        candidates = [(context.layout.work_dir / raw).resolve(strict=False), (context.layout.artifacts_dir / raw).resolve(strict=False)]
        for candidate, root in zip(candidates, (context.layout.work_dir.resolve(), context.layout.artifacts_dir.resolve()), strict=True):
            if candidate != root and root not in candidate.parents:
                continue
            if candidate.exists() and candidate.is_file():
                return candidate
        raise ActionGuardError(f"File is outside allowed roots or does not exist: {value}")

    def _resolve_writable_visible_path(self, context: AgentContext, value: str) -> Path:
        if not value or "\x00" in value:
            raise ActionGuardError("Path must be a non-empty relative path")
        raw = Path(value).expanduser()
        if raw.is_absolute():
            raise ActionGuardError(f"Absolute paths are not allowed: {value}")
        base = context.layout.work_dir
        if raw.parts and raw.parts[0] == "artifacts":
            base = context.layout.artifacts_dir
            raw = Path(*raw.parts[1:]) if len(raw.parts) > 1 else Path("llm-note.txt")
        resolved = (base / raw).resolve(strict=False)
        root = base.resolve()
        if resolved == root or root not in resolved.parents:
            raise ActionGuardError(f"Write path escapes the allowed root: {value}")
        return resolved

    def _initial_observed_paths(self, context: AgentContext) -> set[str]:
        paths = set(context.state.challenge.files)
        self._discover_paths(context, paths)
        return paths

    def _discover_paths(self, context: AgentContext, observed_paths: set[str]) -> None:
        for root in (context.layout.work_dir, context.layout.artifacts_dir):
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if path.is_file():
                    observed_paths.add(self._display_path(context, path))

    def _initial_observed_texts(self, context: AgentContext) -> list[str]:
        challenge = context.state.challenge
        return [
            json.dumps(challenge.to_dict(), ensure_ascii=False, sort_keys=True),
            "\n".join(candidate.value for candidate in context.state.flag_candidates),
        ]

    def _render_prompt(self, context: AgentContext, observations: list[ObservationRecord], observed_paths: set[str]) -> str:
        tools = [tool.to_dict() for tool in context.tool_registry.recommend(context.state.challenge.category, limit=10)]
        trace_tail = [event.to_dict() for event in context.trace_store.read_events()[-12:]]
        payload = {
            "challenge_json": json.dumps(context.state.challenge.to_dict(), ensure_ascii=False, sort_keys=True),
            "tools_json": json.dumps(tools, ensure_ascii=False, sort_keys=True),
            "memory_json": json.dumps(context.metadata.get("memory_matches") or [], ensure_ascii=False, sort_keys=True),
            "brain_context_json": json.dumps(context.metadata.get("brain_context") or {}, ensure_ascii=False, sort_keys=True),
            "relevant_skill_notes_json": json.dumps(context.metadata.get("relevant_skill_notes") or [], ensure_ascii=False, sort_keys=True),
            "structured_observations_json": json.dumps([obs.metadata.get("structured_evidence") for obs in observations[-12:] if obs.metadata.get("structured_evidence")], ensure_ascii=False, sort_keys=True),
            "observations_json": json.dumps([{"summary": obs.summary, "source": obs.source, "metadata": obs.metadata} for obs in observations[-12:]], ensure_ascii=False, sort_keys=True),
            "php_analysis_json": json.dumps(context.metadata.get("php_analysis") or [], ensure_ascii=False, sort_keys=True),
            "trace_json": json.dumps(trace_tail, ensure_ascii=False, sort_keys=True),
            "observed_paths_json": json.dumps(sorted(observed_paths), ensure_ascii=False),
            "flag_candidates_json": json.dumps([candidate.to_dict() for candidate in context.state.flag_candidates], ensure_ascii=False, sort_keys=True),
        }
        return context.prompt_store.render("planner", payload)

    def _record_php_analysis(
        self,
        context: AgentContext,
        text: str,
        *,
        source: str,
        observed_texts: list[str],
        round_index: int,
    ) -> dict[str, object] | None:
        summary = summarize_php_observation(text)
        if not summary:
            return None
        record: dict[str, object] = {"source": source, "round": round_index, **summary}
        analyses = context.metadata.setdefault("php_analysis", [])
        if isinstance(analyses, list) and record not in analyses:
            analyses.append(record)
        raw = json.dumps(record, ensure_ascii=False, sort_keys=True)
        observed_texts.append(raw)
        self._trace(context, "php-analysis", stdout=raw, metadata={"source": source, "round": round_index})
        return record

    def _display_path(self, context: AgentContext, path: Path) -> str:
        resolved = path.resolve(strict=False)
        work_root = context.layout.work_dir.resolve()
        artifacts_root = context.layout.artifacts_dir.resolve()
        if resolved == work_root:
            return "."
        if work_root in resolved.parents:
            return str(resolved.relative_to(work_root))
        if resolved == artifacts_root:
            return "artifacts"
        if artifacts_root in resolved.parents:
            return f"artifacts/{resolved.relative_to(artifacts_root)}"
        return str(path)

    def _complete(self, context: AgentContext, result: LLMActionLoopResult, state: ChallengeState) -> LLMActionLoopResult:
        result.solved = state is ChallengeState.SOLVED
        self._finish_attempt(context.state)
        self._transition(context.state, state)
        self._save(context.state)
        return result

    def _trace(self, context: AgentContext, action: str, **kwargs: object) -> None:
        context.trace_store.append(TraceEvent(challenge_id=context.state.challenge.id, agent="llm-action-loop", action=action, **kwargs))

    def _transition(self, state: ChallengeRunState, next_state: ChallengeState) -> None:
        if state.state is not next_state:
            state.transition_to(next_state)

    def _finish_attempt(self, state: ChallengeRunState) -> None:
        if state.attempts and state.attempts[-1].ended_at is None:
            state.attempts[-1].finish()

    def _save(self, state: ChallengeRunState) -> None:
        if self.save_state is not None:
            self.save_state(state)


def _is_explicit_path_token(token: str) -> bool:
    if token.startswith(("./", "../", "~/", "/")):
        return True
    return "/" in token and not token.startswith("-")


def _line_number(text: str, pattern: str) -> int | None:
    index = text.find(pattern)
    if index < 0:
        return None
    return text.count("\n", 0, index) + 1
