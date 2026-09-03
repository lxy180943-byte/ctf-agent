from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from ctf_agent.core.models import Artifact, FlagCandidate

COMMON_FLAG_PATTERNS = [
    r"flag\{[^}\s]{1,200}\}",
    r"FLAG\{[^}\s]{1,200}\}",
    r"ctf\{[^}\s]{1,200}\}",
    r"CTF\{[^}\s]{1,200}\}",
    r"[A-Za-z0-9_]+CTF\{[^}\s]{1,200}\}",
]


class FlagDetector:
    def __init__(self, flag_regex: str | None = None, custom_patterns: Iterable[str] | None = None) -> None:
        self.patterns: list[tuple[str, str, float]] = []
        if flag_regex:
            self.patterns.append(("flag_regex", flag_regex, 0.99))
        for pattern in custom_patterns or []:
            if pattern and pattern != flag_regex:
                self.patterns.append(("custom", pattern, 0.93))
        for pattern in COMMON_FLAG_PATTERNS:
            if pattern not in [item[1] for item in self.patterns]:
                self.patterns.append(("common", pattern, 0.90))

    def detect_text(self, text: str, source: str) -> list[FlagCandidate]:
        candidates: list[FlagCandidate] = []
        for kind, pattern, confidence in self.patterns:
            try:
                matches = re.finditer(pattern, text)
            except re.error:
                continue
            for match in matches:
                candidates.append(
                    FlagCandidate(
                        value=match.group(0),
                        source=source,
                        confidence=confidence,
                        verified=True,
                        submitted=False,
                        metadata={"pattern": pattern, "pattern_kind": kind},
                    )
                )
        return self.deduplicate(candidates)

    def detect_file(self, path: str | Path, source: str | None = None, limit: int = 1_000_000) -> list[FlagCandidate]:
        file_path = Path(path)
        if not file_path.exists() or not file_path.is_file():
            return []
        text = file_path.read_bytes()[:limit].decode("utf-8", errors="replace")
        return self.detect_text(text, source or f"file:{file_path}")

    def detect_artifacts(self, artifacts: Iterable[Artifact]) -> list[FlagCandidate]:
        candidates: list[FlagCandidate] = []
        for artifact in artifacts:
            if artifact.kind not in {"stdout", "stderr", "report", "text", "challenge-file"}:
                continue
            candidates.extend(self.detect_file(artifact.path, source=f"artifact:{Path(artifact.path).name}"))
        return self.deduplicate(candidates)

    def detect_sources(self, sources: Iterable[tuple[str, str]]) -> list[FlagCandidate]:
        candidates: list[FlagCandidate] = []
        for source, text in sources:
            candidates.extend(self.detect_text(text, source))
        return self.deduplicate(candidates)

    @staticmethod
    def deduplicate(candidates: Iterable[FlagCandidate]) -> list[FlagCandidate]:
        best: dict[str, FlagCandidate] = {}
        for candidate in candidates:
            current = best.get(candidate.value)
            if current is None or candidate.confidence > current.confidence or (
                candidate.confidence == current.confidence and candidate.source < current.source
            ):
                best[candidate.value] = candidate
        return sorted(best.values(), key=lambda item: (-item.confidence, item.value, item.source))
