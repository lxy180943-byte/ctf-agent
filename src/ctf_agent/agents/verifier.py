from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ctf_agent.agents.base import Agent, AgentContext
from ctf_agent.agents.executor import ExecutionBatch
from ctf_agent.core.config import get_nested
from ctf_agent.core.flag_detector import COMMON_FLAG_PATTERNS, FlagDetector
from ctf_agent.core.models import FlagCandidate
from ctf_agent.core.trace import TraceEvent
from ctf_agent.llm import LLMMessage
from ctf_agent.llm.actions import parse_json_object

DEFAULT_FLAG_PATTERNS = COMMON_FLAG_PATTERNS


@dataclass
class VerificationResult:
    candidates: list[FlagCandidate] = field(default_factory=list)

    @property
    def solved(self) -> bool:
        return any(candidate.verified for candidate in self.candidates)


class VerifierAgent(Agent):
    def __init__(self) -> None:
        super().__init__(name="verifier", role="Extract and verify flag candidates from observations and artifacts.")

    def run(self, context: AgentContext) -> VerificationResult:
        batch = context.metadata.get("execution_batch")
        if not isinstance(batch, ExecutionBatch):
            raise ValueError("VerifierAgent requires context.metadata['execution_batch']")

        custom_patterns = get_nested(context.config, ("verification", "custom_flag_patterns")) or []
        detector = FlagDetector(context.state.challenge.flag_regex, custom_patterns=custom_patterns)
        candidates = detector.detect_sources(self._iter_text_sources(batch, context))
        seen: set[str] = set()
        seen.update(candidate.value for candidate in candidates)

        if not candidates and context.llm_provider and context.prompt_store:
            candidates.extend(self._llm_candidates(context, batch, seen))
            candidates = FlagDetector.deduplicate(candidates)

        for candidate in candidates:
            context.state.add_flag_candidate(candidate)

        result = VerificationResult(candidates=candidates)
        context.trace_store.append(
            TraceEvent(
                challenge_id=context.state.challenge.id,
                agent=self.name,
                action="verify",
                stdout="\n".join(candidate.value for candidate in candidates),
                metadata={
                    "solved": result.solved,
                    "candidate_count": len(candidates),
                    "patterns": [pattern for _, pattern, _ in detector.patterns],
                    "source": "regex-or-llm",
                },
            )
        )
        return result

    def _iter_text_sources(self, batch: ExecutionBatch, context: AgentContext | None = None):
        for index, result in enumerate(batch.results, start=1):
            yield f"command:{index}:stdout", result.stdout
            yield f"command:{index}:stderr", result.stderr
            for artifact in result.artifacts:
                if artifact.kind not in {"stdout", "stderr", "report", "text"}:
                    continue
                path = Path(artifact.path)
                if not path.exists() or not path.is_file():
                    continue
                yield f"artifact:{path.name}", path.read_text(encoding="utf-8", errors="replace")[:1_000_000]
        if context is not None:
            for file_name in context.state.challenge.files:
                path = context.layout.work_dir / file_name
                if path.exists() and path.is_file():
                    yield f"file:{file_name}", path.read_bytes()[:1_000_000].decode("utf-8", errors="replace")

    def _llm_candidates(self, context: AgentContext, batch: ExecutionBatch, seen: set[str]) -> list[FlagCandidate]:
        assert context.llm_provider is not None
        assert context.prompt_store is not None
        sources = [{"source": source, "text": text[:8000]} for source, text in self._iter_text_sources(batch, context)]
        prompt = context.prompt_store.render(
            "verifier",
            {
                "challenge_json": json.dumps(context.state.challenge.to_dict(), ensure_ascii=False, sort_keys=True),
                "observation_json": json.dumps(sources, ensure_ascii=False, sort_keys=True),
            },
        )
        try:
            response = context.llm_provider.complete(
                messages=[
                    LLMMessage(role="system", content="Return only JSON flag candidates that are exactly observed."),
                    LLMMessage(role="user", content=prompt),
                ],
                response_format="json_object",
            )
            data = parse_json_object(response.content)
        except Exception as exc:
            context.trace_store.append(
                TraceEvent(
                    challenge_id=context.state.challenge.id,
                    agent=self.name,
                    action="llm-verify-fallback",
                    stderr=str(exc),
                    metadata={"reason": "LLM verifier failed; keeping regex result"},
                )
            )
            return []

        candidates: list[FlagCandidate] = []
        raw_candidates = data.get("candidates", [])
        if not isinstance(raw_candidates, list):
            return []
        observed_text = "\n".join(item["text"] for item in sources)
        for item in raw_candidates:
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            if not isinstance(value, str) or not value or value in seen or value not in observed_text:
                continue
            seen.add(value)
            candidates.append(
                FlagCandidate(
                    value=value,
                    source=str(item.get("source") or "llm-verifier"),
                    confidence=float(item.get("confidence", 0.5)),
                    verified=bool(item.get("verified", False)),
                    submitted=False,
                    metadata={"source": "llm", "provider": context.llm_provider.name},
                )
            )
        return candidates
