from __future__ import annotations

from pathlib import Path
from typing import Any

from ctf_agent.evals.base import BenchmarkAdapter, BenchmarkChallenge, evaluator_private_metadata, sanitize_benchmark_for_solver
from ctf_agent.platforms.local import LocalPlatformAdapter


class LocalBenchmark(BenchmarkAdapter):
    name = "local"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()
        self.adapter = LocalPlatformAdapter(self.root)

    def list_challenges(self) -> list[BenchmarkChallenge]:
        challenges = []
        for challenge in self.adapter.list_challenges():
            metadata = challenge.metadata
            expected_flags = _string_list(metadata.get("expected_flags", metadata.get("expected_flag")))
            public_metadata = {"benchmark": self.name, "dataset_root": str(self.root), **_benchmark_metadata(metadata)}
            private_metadata = evaluator_private_metadata(metadata, expected_flags)
            item = BenchmarkChallenge(
                challenge=challenge,
                adapter=LocalPlatformAdapter(metadata.get("source_dir", self.root)),
                expected_flags=expected_flags,
                max_time=_optional_float(metadata.get("max_time")),
                difficulty=str(metadata.get("difficulty") or "unknown"),
                tags=_string_list(metadata.get("tags")),
                required_tools=_string_list(metadata.get("required_tools")),
                metadata=public_metadata,
                evaluator_metadata=private_metadata,
            )
            item.challenge = sanitize_benchmark_for_solver(item)
            challenges.append(item)
        return challenges


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _benchmark_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    keys = ("difficulty", "tags", "required_tools", "max_time", "source_benchmark", "benchmark_type", "expected_flag_isolated")
    return {key: metadata[key] for key in keys if key in metadata}
