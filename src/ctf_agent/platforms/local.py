from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ctf_agent.core.config import parse_simple_yaml
from ctf_agent.core.models import Artifact, Challenge
from ctf_agent.platforms.base import PlatformAdapter, SubmissionResult

CHALLENGE_YAML_NAMES = ("challenge.yaml", "challenge.yml")


class LocalPlatformAdapter(PlatformAdapter):
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()

    def list_challenges(self) -> list[Challenge]:
        if not self.root.exists():
            raise FileNotFoundError(f"Local challenge path does not exist: {self.root}")
        if self._metadata_path(self.root):
            return [self._load_challenge_dir(self.root)]

        challenges: list[Challenge] = []
        for child in sorted(self.root.iterdir(), key=lambda item: item.name):
            if child.name.startswith(".") or not child.is_dir():
                continue
            challenges.append(self._load_challenge_dir(child))
        return challenges

    def get_challenge(self, challenge_id: str) -> Challenge:
        candidate = Path(challenge_id).expanduser()
        if candidate.exists():
            return self._load_challenge_path(candidate)

        if self.root.name == challenge_id or self.root.name.replace(" ", "-") == challenge_id:
            return self._load_challenge_dir(self.root)

        for challenge in self.list_challenges():
            if challenge.id == challenge_id or challenge.title == challenge_id:
                return challenge
        raise KeyError(f"Challenge not found: {challenge_id}")

    def download_files(self, challenge: Challenge, destination: str | Path) -> list[Artifact]:
        destination_path = Path(destination).expanduser()
        destination_path.mkdir(parents=True, exist_ok=True)
        source_dir = Path(str(challenge.metadata.get("source_dir", self.root))).expanduser()

        artifacts: list[Artifact] = []
        for file_name in challenge.files:
            source = (source_dir / file_name).resolve()
            if not source.exists() or not source.is_file():
                raise FileNotFoundError(f"Challenge file not found: {source}")
            target = destination_path / Path(file_name).name
            shutil.copy2(source, target)
            artifacts.append(
                Artifact(
                    path=str(target),
                    kind="challenge-file",
                    description=f"Downloaded local challenge file {file_name}",
                    metadata={"source": str(source)},
                )
            )
        return artifacts

    def submit_flag(self, challenge: Challenge, flag: str, *, submit: bool = False) -> SubmissionResult:
        return SubmissionResult(
            challenge_id=challenge.id,
            flag=flag,
            submitted=False,
            accepted=None,
            message="local adapter does not submit flags; dry-run only",
            metadata={"requested_submit": submit},
        )

    def _load_challenge_path(self, path: Path) -> Challenge:
        if path.is_file() and path.name in CHALLENGE_YAML_NAMES:
            return self._load_challenge_dir(path.parent)
        if path.is_dir():
            return self._load_challenge_dir(path)
        raise ValueError(f"Unsupported local challenge path: {path}")

    def _load_challenge_dir(self, challenge_dir: Path) -> Challenge:
        metadata_path = self._metadata_path(challenge_dir)
        if metadata_path:
            data = parse_simple_yaml(metadata_path.read_text(encoding="utf-8"))
            return self._challenge_from_yaml(challenge_dir, data)
        return self._challenge_from_directory(challenge_dir)

    def _challenge_from_yaml(self, challenge_dir: Path, data: dict[str, Any]) -> Challenge:
        files = self._normalize_files(data.get("files", []))
        return Challenge(
            id=str(data.get("id") or challenge_dir.name),
            title=str(data.get("title") or challenge_dir.name),
            category=str(data.get("category") or "misc"),
            description=str(data.get("description") or ""),
            files=files,
            connection=data.get("connection"),
            hints=[str(item) for item in data.get("hints", [])],
            flag_regex=data.get("flag_regex"),
            metadata={**dict(data.get("metadata", {})), "source": "local", "source_dir": str(challenge_dir)},
        )

    def _challenge_from_directory(self, challenge_dir: Path) -> Challenge:
        files = [
            str(path.relative_to(challenge_dir))
            for path in sorted(challenge_dir.rglob("*"), key=lambda item: str(item))
            if path.is_file() and path.name not in CHALLENGE_YAML_NAMES
        ]
        return Challenge(
            id=challenge_dir.name,
            title=challenge_dir.name.replace("-", " ").replace("_", " ").title(),
            category="misc",
            files=files,
            metadata={"source": "local", "source_dir": str(challenge_dir), "inferred": True},
        )

    def _metadata_path(self, challenge_dir: Path) -> Path | None:
        for name in CHALLENGE_YAML_NAMES:
            candidate = challenge_dir / name
            if candidate.is_file():
                return candidate
        return None

    def _normalize_files(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(item) for item in value]
        raise ValueError("challenge.yaml field 'files' must be a string or list")
