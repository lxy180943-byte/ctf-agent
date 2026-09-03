from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from ctf_agent.core.config import get_nested
from ctf_agent.core.models import utc_now
from ctf_agent.core.state import ChallengeRunState, ChallengeState
from ctf_agent.core.trace import TraceEvent, TraceStore
from ctf_agent.core.workspace import WorkspaceManager


@dataclass
class KnowledgeItem:
    category: str
    pattern: str
    symptom: str
    solution: str
    commands: list[str]
    source_run: str
    confidence: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)
    success_count: int = 0
    failure_count: int = 0
    last_used: str | None = None
    source_type: str = "real"
    created_at: str = field(default_factory=utc_now)
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "pattern": self.pattern,
            "symptom": self.symptom,
            "solution": self.solution,
            "commands": list(self.commands),
            "source_run": self.source_run,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_used": self.last_used,
            "source_type": self.source_type,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnowledgeItem:
        return cls(
            id=str(data.get("id") or uuid4().hex),
            category=str(data["category"]),
            pattern=str(data["pattern"]),
            symptom=str(data["symptom"]),
            solution=str(data["solution"]),
            commands=[str(item) for item in data.get("commands", [])],
            source_run=str(data["source_run"]),
            confidence=float(data.get("confidence", 0.5)),
            metadata=dict(data.get("metadata", {})),
            success_count=int(data.get("success_count", 0) or 0),
            failure_count=int(data.get("failure_count", 0) or 0),
            last_used=data.get("last_used"),
            source_type=str(data.get("source_type") or data.get("metadata", {}).get("experience_scope") or data.get("metadata", {}).get("kind") or "real"),
            created_at=str(data.get("created_at", utc_now())),
        )


class MemoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._ensure_schema()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> MemoryStore:
        configured = get_nested(config, ("memory", "path"))
        if configured:
            return cls(configured)
        workspace_dir = Path(get_nested(config, ("workspace_dir",)) or "~/ctf-workspace").expanduser()
        return cls(workspace_dir / "memory" / "knowledge.sqlite")

    def add(self, item: KnowledgeItem) -> KnowledgeItem:
        if not item.source_run.strip():
            raise ValueError("KnowledgeItem.source_run is required for traceability")
        if not item.id:
            item.id = uuid4().hex
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id, confidence, commands_json, metadata_json, success_count, failure_count, last_used, source_type
                FROM knowledge
                WHERE category = ? AND pattern = ? AND symptom = ? AND solution = ? AND source_run = ?
                """,
                (item.category, item.pattern, item.symptom, item.solution, item.source_run),
            ).fetchone()
            if existing:
                item.id = str(existing["id"])
                item.confidence = max(float(existing["confidence"]), item.confidence)
                item.commands = _merge_lists(json.loads(existing["commands_json"]), item.commands)
                item.metadata = {**json.loads(existing["metadata_json"]), **item.metadata}
                item.success_count = max(int(existing["success_count"] or 0), item.success_count)
                item.failure_count = max(int(existing["failure_count"] or 0), item.failure_count)
                item.last_used = item.last_used or existing["last_used"]
                item.source_type = item.source_type or str(existing["source_type"] or "real")
            connection.execute(
                """
                INSERT INTO knowledge (
                    id, category, pattern, symptom, solution, commands_json,
                    source_run, confidence, metadata_json, success_count,
                    failure_count, last_used, source_type, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    category = excluded.category,
                    pattern = excluded.pattern,
                    symptom = excluded.symptom,
                    solution = excluded.solution,
                    commands_json = excluded.commands_json,
                    source_run = excluded.source_run,
                    confidence = excluded.confidence,
                    metadata_json = excluded.metadata_json,
                    success_count = excluded.success_count,
                    failure_count = excluded.failure_count,
                    last_used = excluded.last_used,
                    source_type = excluded.source_type
                """,
                (
                    item.id,
                    item.category,
                    item.pattern,
                    item.symptom,
                    item.solution,
                    json.dumps(item.commands, ensure_ascii=False),
                    item.source_run,
                    item.confidence,
                    json.dumps(item.metadata, ensure_ascii=False, sort_keys=True),
                    item.success_count,
                    item.failure_count,
                    item.last_used,
                    item.source_type,
                    item.created_at,
                ),
            )
        return item

    def get(self, item_id: str) -> KnowledgeItem | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM knowledge WHERE id = ?", (item_id,)).fetchone()
        return _row_to_item(row) if row else None

    def list(self, *, category: str | None = None, limit: int = 50) -> list[KnowledgeItem]:
        with self._connect() as connection:
            if category:
                rows = connection.execute(
                    "SELECT * FROM knowledge WHERE category = ? ORDER BY created_at DESC LIMIT ?",
                    (category, limit),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM knowledge ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_item(row) for row in rows]

    def search(
        self,
        query: str = "",
        *,
        category: str | None = None,
        limit: int = 10,
        file_magic: Iterable[str] | None = None,
        available_tools: Iterable[str] | None = None,
    ) -> list[KnowledgeItem]:
        items = self.list(category=category, limit=1000)
        tokens = _tokens(query)
        magic_tokens = _tokens(" ".join(str(item) for item in (file_magic or [])))
        available = {str(tool).lower() for tool in (available_tools or [])}
        scored: list[tuple[float, KnowledgeItem]] = []
        for item in items:
            score = _quality_score(item)
            text = _search_text(item)
            matched = not tokens and not magic_tokens
            if category and item.category == category:
                score += 1.5
                matched = True
            for token in tokens:
                if token in text:
                    score += 1.0
                    matched = True
            for token in magic_tokens:
                if token in text:
                    score += 0.75
                    matched = True
            tool_overlap = _tool_overlap(item.commands, available)
            score += tool_overlap * 0.4
            if available and item.commands and tool_overlap == 0:
                score -= 0.25
            if item.source_type == "benchmark":
                score -= 0.05
            if item.source_type == "failure-retrospective" or item.metadata.get("kind") == "failure-retrospective":
                score -= 0.3
            if matched or tool_overlap:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], -pair[1].success_count, pair[1].failure_count, -pair[1].confidence, pair[1].created_at))
        return [item for _, item in scored[:limit]]

    def mark_used(self, item_ids: Iterable[str]) -> None:
        ids = [str(item_id) for item_id in item_ids if str(item_id)]
        if not ids:
            return
        now = utc_now()
        with self._connect() as connection:
            connection.executemany("UPDATE knowledge SET last_used = ? WHERE id = ?", [(now, item_id) for item_id in ids])

    def promote(self, item_id: str, *, amount: float = 0.08) -> KnowledgeItem:
        item = self.get(item_id)
        if item is None:
            raise KeyError(f"Knowledge item not found: {item_id}")
        confidence = min(0.99, item.confidence + amount)
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE knowledge SET confidence = ?, success_count = success_count + 1, last_used = ? WHERE id = ?",
                (confidence, now, item_id),
            )
        updated = self.get(item_id)
        assert updated is not None
        return updated

    def demote(self, item_id: str, *, amount: float = 0.08) -> KnowledgeItem:
        item = self.get(item_id)
        if item is None:
            raise KeyError(f"Knowledge item not found: {item_id}")
        confidence = max(0.05, item.confidence - amount)
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE knowledge SET confidence = ?, failure_count = failure_count + 1, last_used = ? WHERE id = ?",
                (confidence, now, item_id),
            )
        updated = self.get(item_id)
        assert updated is not None
        return updated

    def prune(self, *, min_confidence: float = 0.2, source_type: str | None = None, include_successful: bool = False) -> int:
        clauses = ["confidence < ?"]
        params: list[Any] = [min_confidence]
        if source_type:
            clauses.append("source_type = ?")
            params.append(source_type)
        if not include_successful:
            clauses.append("success_count = 0")
        query = "DELETE FROM knowledge WHERE " + " AND ".join(clauses)
        with self._connect() as connection:
            cursor = connection.execute(query, params)
            return int(cursor.rowcount or 0)

    def learn_from_run(self, run_dir: str | Path) -> list[KnowledgeItem]:
        run_path = Path(run_dir).expanduser().resolve()
        workspace_root = run_path.parent.parent if run_path.parent.name == "runs" else run_path.parent.parent
        manager = WorkspaceManager(workspace_root)
        state = manager.load_state(run_path.name)
        events = _events_for_state(state, TraceStore(run_path / "trace.jsonl").read_events())
        writeup = run_path / "writeup.md"
        writeup_text = writeup.read_text(encoding="utf-8", errors="replace")[:20_000] if writeup.exists() else ""
        items = self._items_from_run(state, events, run_path, writeup_text)
        return [self.add(item) for item in items]

    def _items_from_run(
        self,
        state: ChallengeRunState,
        events: list[TraceEvent],
        run_path: Path,
        writeup_text: str,
    ) -> list[KnowledgeItem]:
        items: list[KnowledgeItem] = []
        commands = _key_commands(events, successful_only=True)
        all_commands = _key_commands(events, successful_only=False)
        invalid_commands = _invalid_commands(events)
        source_run = str(run_path)
        category = state.challenge.category or state.metadata.get("classification", {}).get("category") or "misc"
        pattern = _pattern_from_state(state)
        symptom = _symptom_from_state(state)

        if state.state is ChallengeState.SOLVED and commands:
            best_confidence = max([candidate.confidence for candidate in state.flag_candidates if candidate.verified] or [0.8])
            items.append(
                KnowledgeItem(
                    category=category,
                    pattern=pattern,
                    symptom=symptom,
                    solution=_solution_summary(state, events, writeup_text),
                    commands=commands[:8],
                    source_run=source_run,
                    confidence=max(0.7, min(0.99, best_confidence)),
                    success_count=1,
                    source_type="real",
                    metadata={
                        "kind": "solved-route",
                        "source_type": "real",
                        "challenge_id": state.challenge.id,
                        "title": state.challenge.title,
                        "classification": state.metadata.get("classification"),
                        "flag_count": len([candidate for candidate in state.flag_candidates if candidate.verified]),
                    },
                )
            )

        if invalid_commands or state.state is ChallengeState.FAILED or state.metadata.get("failure_count"):
            items.append(
                KnowledgeItem(
                    category=category,
                    pattern=f"failure-retrospective: {pattern}",
                    symptom=_failure_symptom(state, events, invalid_commands),
                    solution=_failure_suggestion(state, invalid_commands),
                    commands=invalid_commands[:8] or all_commands[:5],
                    source_run=source_run,
                    confidence=0.35 if state.state is ChallengeState.FAILED else 0.25,
                    failure_count=1,
                    source_type="failure-retrospective",
                    metadata={
                        "kind": "failure-retrospective",
                        "source_type": "failure-retrospective",
                        "challenge_id": state.challenge.id,
                        "wrong_hypotheses": _failure_messages(events),
                        "invalid_commands": invalid_commands,
                        "next_suggestions": _next_suggestions(state, invalid_commands),
                    },
                )
            )
        return items

    def _ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    symptom TEXT NOT NULL,
                    solution TEXT NOT NULL,
                    commands_json TEXT NOT NULL,
                    source_run TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(knowledge)").fetchall()}
            for column, definition in {
                "success_count": "INTEGER NOT NULL DEFAULT 0",
                "failure_count": "INTEGER NOT NULL DEFAULT 0",
                "last_used": "TEXT",
                "source_type": "TEXT NOT NULL DEFAULT 'real'",
            }.items():
                if column not in columns:
                    connection.execute(f"ALTER TABLE knowledge ADD COLUMN {column} {definition}")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge(category)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_source_run ON knowledge(source_run)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_source_type ON knowledge(source_type)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


def _row_to_item(row: sqlite3.Row) -> KnowledgeItem:
    return KnowledgeItem(
        id=str(row["id"]),
        category=str(row["category"]),
        pattern=str(row["pattern"]),
        symptom=str(row["symptom"]),
        solution=str(row["solution"]),
        commands=[str(item) for item in json.loads(row["commands_json"])],
        source_run=str(row["source_run"]),
        confidence=float(row["confidence"]),
        metadata=dict(json.loads(row["metadata_json"])),
        success_count=int(row["success_count"] or 0),
        failure_count=int(row["failure_count"] or 0),
        last_used=row["last_used"],
        source_type=str(row["source_type"] or "real"),
        created_at=str(row["created_at"]),
    )


def _events_for_state(state: ChallengeRunState, events: list[TraceEvent]) -> list[TraceEvent]:
    return [event for event in events if event.timestamp >= state.created_at]


def _tokens(value: str) -> list[str]:
    return [token.lower() for token in re.findall(r"[A-Za-z0-9_./:-]+", value) if token]


def _search_text(item: KnowledgeItem) -> str:
    return " ".join(
        [
            item.category,
            item.pattern,
            item.symptom,
            item.solution,
            " ".join(item.commands),
            json.dumps(item.metadata, ensure_ascii=False, sort_keys=True),
        ]
    ).lower()


def _quality_score(item: KnowledgeItem) -> float:
    return item.confidence + min(item.success_count, 10) * 0.12 - min(item.failure_count, 10) * 0.08


def _tool_overlap(commands: Iterable[str], available_tools: set[str]) -> int:
    if not available_tools:
        return 0
    overlap = 0
    for command in commands:
        first = command.strip().split(" ", 1)[0] if command.strip() else ""
        if Path(first).name.lower() in available_tools:
            overlap += 1
    return overlap


def _merge_lists(first: Iterable[str], second: Iterable[str]) -> list[str]:
    merged: list[str] = []
    for value in list(first) + list(second):
        if value not in merged:
            merged.append(str(value))
    return merged


def _format_command(command: list[str] | None) -> str:
    if not command:
        return ""
    if len(command) >= 3 and command[0] in {"bash", "docker"} and command[-2] == "-lc":
        return command[-1]
    return " ".join(command)


def _key_commands(events: list[TraceEvent], *, successful_only: bool) -> list[str]:
    commands: list[str] = []
    for event in events:
        if event.agent != "executor" or event.action != "run-command":
            continue
        if successful_only and event.exit_code != 0:
            continue
        command = _format_command(event.command)
        if command and command not in commands:
            commands.append(command)
    return commands


def _invalid_commands(events: list[TraceEvent]) -> list[str]:
    invalid: list[str] = []
    for event in events:
        if event.agent != "executor" or event.action != "run-command":
            continue
        timed_out = bool(event.metadata.get("timed_out")) if isinstance(event.metadata, dict) else False
        if event.exit_code not in (None, 0) or timed_out:
            command = _format_command(event.command)
            if command and command not in invalid:
                invalid.append(command)
    return invalid


def _pattern_from_state(state: ChallengeRunState) -> str:
    parts = [state.challenge.title, state.challenge.category]
    if state.challenge.files:
        suffixes = [Path(file_name).suffix or Path(file_name).name for file_name in state.challenge.files]
        parts.append("files=" + ",".join(suffixes))
    classification = state.metadata.get("classification")
    if isinstance(classification, dict) and classification.get("category"):
        parts.append(f"classified={classification['category']}")
    return " | ".join(part for part in parts if part)


def _symptom_from_state(state: ChallengeRunState) -> str:
    bits = [state.challenge.description]
    bits.extend(state.challenge.hints[:3])
    return " ".join(bit.strip() for bit in bits if bit and bit.strip())[:1000] or "No description or hint recorded."


def _solution_summary(state: ChallengeRunState, events: list[TraceEvent], writeup_text: str) -> str:
    if writeup_text:
        for marker in ("## Reproduction Steps", "## Key Commands"):
            if marker in writeup_text:
                return f"Solved route is documented in writeup; key section starts at {marker}."
    flag_sources = [candidate.source for candidate in state.flag_candidates if candidate.verified]
    command_count = len(_key_commands(events, successful_only=True))
    return f"Solved by collecting observations from {command_count} successful command(s) and verifying flag candidate sources: {flag_sources}."


def _failure_messages(events: list[TraceEvent]) -> list[str]:
    messages: list[str] = []
    for event in events:
        if "failure" in event.action or event.agent == "critic":
            detail = event.stdout or event.stderr or event.action
            if detail and detail.strip() not in messages:
                messages.append(detail.strip())
        bus = event.metadata.get("message_bus") if isinstance(event.metadata, dict) else None
        if isinstance(bus, dict):
            for message in bus.get("messages", []):
                if isinstance(message, dict) and message.get("kind") == "failure_reason":
                    content = str(message.get("content") or "").strip()
                    if content and content not in messages:
                        messages.append(content)
    return messages


def _failure_symptom(state: ChallengeRunState, events: list[TraceEvent], invalid_commands: list[str]) -> str:
    messages = _failure_messages(events)
    if messages:
        return " | ".join(messages)[:1000]
    if invalid_commands:
        return f"{len(invalid_commands)} command(s) failed or timed out during this route."
    return f"Run ended in state {state.state.value} without a verified flag candidate."


def _failure_suggestion(state: ChallengeRunState, invalid_commands: list[str]) -> str:
    suggestions = _next_suggestions(state, invalid_commands)
    return " ".join(suggestions)


def _next_suggestions(state: ChallengeRunState, invalid_commands: list[str]) -> list[str]:
    suggestions = []
    if invalid_commands:
        suggestions.append("Verify file names, cwd, and tool availability before reusing failed commands.")
    if state.challenge.files:
        suggestions.append("Use a broad non-destructive workspace scan before switching to expensive specialist tooling.")
    else:
        suggestions.append("Re-check the challenge metadata and add missing local files before planning more commands.")
    suggestions.append("Keep the next plan short and validate each observation before assuming a route is correct.")
    return suggestions
