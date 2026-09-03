from __future__ import annotations

import json
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ctf_agent.core.models import Challenge
from ctf_agent.platforms.base import PlatformAdapter

EVALUATOR_ONLY_KEY_PARTS = (
    "expected_flag",
    "expected_flags",
    "answer",
    "solution",
    "ground_truth",
    "evaluator_only",
)
EVALUATOR_ONLY_FILE_KEYS = {
    "answer",
    "answer_file",
    "answer_files",
    "solution",
    "solution_file",
    "solution_files",
    "ground_truth",
    "ground_truth_file",
    "ground_truth_files",
    "evaluator_only_file",
    "evaluator_only_files",
    "evaluator_only_artifact",
    "evaluator_only_artifacts",
}


@dataclass
class BenchmarkChallenge:
    challenge: Challenge
    adapter: PlatformAdapter
    expected_flags: list[str] = field(default_factory=list)
    max_time: float | None = None
    difficulty: str = "unknown"
    tags: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    evaluator_metadata: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "challenge": self.challenge.to_dict(),
            "expected_flag_count": len(self.expected_flags),
            "max_time": self.max_time,
            "category": self.challenge.category,
            "difficulty": self.difficulty,
            "tags": list(self.tags),
            "required_tools": list(self.required_tools),
            "metadata": dict(self.metadata),
        }
        if not self.metadata.get("expected_flag_isolated"):
            data["expected_flags"] = list(self.expected_flags)
        return data


@dataclass
class ExternalBenchmarkRecord:
    benchmark: str
    challenge_id: str
    title: str
    category: str
    description: str = ""
    files: list[str] = field(default_factory=list)
    expected_flags: list[str] = field(default_factory=list)
    difficulty: str = "unknown"
    tags: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_local_yaml(self) -> str:
        lines = [
            f"id: {self.challenge_id}",
            f"title: {self.title}",
            f"category: {self.category}",
            f"description: {self.description}",
            "files:",
        ]
        lines.extend(f"  - {file_name}" for file_name in self.files)
        lines.extend(
            [
                "metadata:",
                f"  expected_flags: [{', '.join(self.expected_flags)}]",
                f"  difficulty: {self.difficulty}",
                f"  tags: [{', '.join(self.tags)}]",
                f"  required_tools: [{', '.join(self.required_tools)}]",
                f"  source_benchmark: {self.benchmark}",
            ]
        )
        return "\n".join(lines) + "\n"


def sanitize_benchmark_for_solver(benchmark_case: BenchmarkChallenge) -> Challenge:
    """Return the Challenge visible to solver code, excluding evaluator-only data."""

    challenge = benchmark_case.challenge
    expected_flags = list(benchmark_case.expected_flags)
    evaluator_files = _evaluator_only_files(challenge.metadata, benchmark_case.metadata, benchmark_case.evaluator_metadata)
    files = [file_name for file_name in challenge.files if not _is_evaluator_file(file_name, evaluator_files)]
    return Challenge(
        id=challenge.id,
        title=_remove_expected_values(challenge.title, expected_flags),
        category=challenge.category,
        description=_remove_expected_values(challenge.description, expected_flags),
        files=files,
        connection=challenge.connection,
        hints=[],
        flag_regex=None if _contains_expected_value(challenge.flag_regex, expected_flags) else challenge.flag_regex,
        metadata=_sanitize_solver_metadata(challenge.metadata, expected_flags),
    )


def evaluator_private_metadata(metadata: Mapping[str, Any], expected_flags: list[str]) -> dict[str, Any]:
    private = {"expected_flags": list(expected_flags)}
    for key, value in metadata.items():
        if _is_evaluator_key(key):
            private[str(key)] = value
    return private


def _sanitize_solver_metadata(value: Any, expected_flags: list[str]) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if _is_evaluator_key(key):
                continue
            cleaned = _sanitize_solver_metadata(item, expected_flags)
            if cleaned not in (None, "", [], {}):
                result[str(key)] = cleaned
        return result
    if isinstance(value, list):
        cleaned_items = [_sanitize_solver_metadata(item, expected_flags) for item in value]
        return [item for item in cleaned_items if item not in (None, "", [], {})]
    if isinstance(value, str):
        cleaned = _remove_expected_values(value, expected_flags)
        return "" if _contains_evaluator_word(cleaned) else cleaned
    return value


def _evaluator_only_files(*metadata_sources: Mapping[str, Any]) -> set[str]:
    files: set[str] = set()
    for metadata in metadata_sources:
        for key, value in metadata.items():
            if str(key).lower() in EVALUATOR_ONLY_FILE_KEYS:
                files.update(_string_values(value))
            elif str(key).lower() == "evaluator_only" and isinstance(value, Mapping):
                for nested_key, nested_value in value.items():
                    if str(nested_key).lower() in EVALUATOR_ONLY_FILE_KEYS or "file" in str(nested_key).lower() or "artifact" in str(nested_key).lower():
                        files.update(_string_values(nested_value))
    return files


def _is_evaluator_file(file_name: str, evaluator_files: set[str]) -> bool:
    normalized = str(Path(file_name).as_posix()).lstrip("./")
    basename = Path(normalized).name
    return normalized in evaluator_files or basename in evaluator_files


def _string_values(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, Mapping):
        values: list[str] = []
        for item in value.values():
            values.extend(_string_values(item))
        return values
    if isinstance(value, (list, tuple, set)):
        values: list[str] = []
        for item in value:
            values.extend(_string_values(item))
        return values
    return [str(value), Path(str(value)).name]


def _is_evaluator_key(key: object) -> bool:
    lowered = str(key).lower().replace("-", "_")
    return any(part in lowered for part in EVALUATOR_ONLY_KEY_PARTS)


def _contains_evaluator_word(value: str) -> bool:
    lowered = value.lower().replace("-", "_")
    return any(part in lowered for part in ("answer", "solution", "ground_truth"))


def _contains_expected_value(value: str | None, expected_flags: list[str]) -> bool:
    text = str(value or "")
    return any(flag and flag in text for flag in expected_flags)


def _remove_expected_values(value: str, expected_flags: list[str]) -> str:
    text = str(value or "")
    for flag in expected_flags:
        if flag:
            text = text.replace(flag, "<expected-flag-redacted>")
    return text


class BenchmarkAdapter(ABC):
    name: str

    @abstractmethod
    def list_challenges(self) -> list[BenchmarkChallenge]:
        raise NotImplementedError

    def convert_to_local_dataset(self, output_dir: str | Path) -> Path:
        records = self.export_records()
        output_path = Path(output_dir).expanduser()
        output_path.mkdir(parents=True, exist_ok=True)
        for record in records:
            challenge_dir = output_path / record.challenge_id
            challenge_dir.mkdir(parents=True, exist_ok=True)
            (challenge_dir / "challenge.yaml").write_text(record.to_local_yaml(), encoding="utf-8")
            self.copy_record_files(record, challenge_dir)
        return output_path

    def export_records(self) -> list[ExternalBenchmarkRecord]:
        raise NotImplementedError(f"{self.name} export is a format-conversion skeleton; implement dataset-specific parsing next.")

    def copy_record_files(self, record: ExternalBenchmarkRecord, challenge_dir: Path) -> None:
        root = getattr(self, "root", None)
        if not root:
            return
        root_path = Path(root)
        for file_name in record.files:
            source = root_path / file_name
            if source.exists() and source.is_file():
                target = challenge_dir / Path(file_name).name
                shutil.copy2(source, target)


class UnsupportedBenchmarkAdapter(BenchmarkAdapter):
    def __init__(self, name: str, root: str | Path | None = None) -> None:
        self.name = name
        self.root = Path(root).expanduser() if root else None

    def list_challenges(self) -> list[BenchmarkChallenge]:
        raise NotImplementedError(f"{self.name} adapter is reserved for a future external benchmark integration.")

    def export_records(self) -> list[ExternalBenchmarkRecord]:
        if self.root is None:
            raise NotImplementedError(f"{self.name} conversion skeleton requires a dataset root.")
        manifest = self.root / "manifest.jsonl"
        if not manifest.exists():
            raise NotImplementedError(f"{self.name} conversion skeleton expects manifest.jsonl or a dataset-specific parser.")
        records: list[ExternalBenchmarkRecord] = []
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            records.append(
                ExternalBenchmarkRecord(
                    benchmark=self.name,
                    challenge_id=str(data.get("id") or data.get("challenge_id")),
                    title=str(data.get("title") or data.get("name") or data.get("id")),
                    category=str(data.get("category") or "misc"),
                    description=str(data.get("description") or ""),
                    files=[str(item) for item in data.get("files", [])],
                    expected_flags=[str(item) for item in data.get("expected_flags", [])],
                    difficulty=str(data.get("difficulty") or "unknown"),
                    tags=[str(item) for item in data.get("tags", [])],
                    required_tools=[str(item) for item in data.get("required_tools", [])],
                    metadata={"raw": data},
                )
            )
        return records


class CybenchAdapter(UnsupportedBenchmarkAdapter):
    def __init__(self, root: str | Path | None = None) -> None:
        super().__init__("cybench", root)


class NYUCTFBenchAdapter(UnsupportedBenchmarkAdapter):
    def __init__(self, root: str | Path | None = None) -> None:
        super().__init__("nyu-ctf-bench", root)


class CyberZeroAdapter(UnsupportedBenchmarkAdapter):
    def __init__(self, root: str | Path | None = None) -> None:
        super().__init__("cyber-zero", root)
