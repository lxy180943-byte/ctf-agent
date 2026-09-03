from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_:+./-]+")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_CATEGORIES = ("ctf-web", "ctf-pwn", "ctf-crypto", "ctf-reverse", "ctf-forensics")
_DEFAULT_SNIPPET_LIMIT = 700
_QUERY_ALIASES = {"php特性": ("type juggling", "parse_str", "md5 array", "intval", "in_array", "strpos"), "文件包含": ("include", "lfi", "php://filter"), "源码泄露": ("source disclosure", "highlight_file")}


@dataclass(frozen=True)
class SkillNote:
    category: str
    title: str
    source: str
    snippet: str
    matched_terms: tuple[str, ...]
    score: int

    def to_dict(self) -> dict[str, Any]:
        return {"category": self.category, "title": self.title, "source": self.source, "snippet": self.snippet, "matched_terms": list(self.matched_terms), "score": self.score}


@dataclass(frozen=True)
class _Chunk:
    category: str
    title: str
    source: Path
    text: str


class SkillIndex:
    """Offline Markdown retriever that supplies bounded context notes."""

    def __init__(self, root: str | Path | None, *, snippet_limit: int = _DEFAULT_SNIPPET_LIMIT, max_files: int = 500) -> None:
        self.root = Path(root).expanduser() if root else None
        self.snippet_limit = max(120, int(snippet_limit))
        self.max_files = max(1, int(max_files))
        self._chunks: list[_Chunk] | None = None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "SkillIndex":
        knowledge = config.get("knowledge")
        knowledge = knowledge if isinstance(knowledge, dict) else {}
        return cls(knowledge.get("skill_docs"), snippet_limit=knowledge.get("snippet_limit", _DEFAULT_SNIPPET_LIMIT), max_files=knowledge.get("max_files", 500))

    @property
    def available(self) -> bool:
        return self.root is not None and self.root.is_dir()

    def search(self, query: str | Iterable[str], *, category: str | None = None, limit: int = 6) -> list[dict[str, Any]]:
        terms = self._terms(query)
        if not terms or not self.available:
            return []
        wanted = category.lower() if category else None
        results: list[SkillNote] = []
        for chunk in self._load_chunks():
            if wanted and wanted not in {chunk.category, chunk.category.removeprefix("ctf-")}:
                continue
            haystack = self._terms(f"{chunk.title} {chunk.text}")
            matched = tuple(sorted(term for term in terms if term in haystack))
            if not matched:
                continue
            title_terms = self._terms(chunk.title)
            score = sum(2 if term in title_terms else 1 for term in matched)
            results.append(SkillNote(chunk.category, chunk.title, str(chunk.source.relative_to(self.root)), self._snippet(chunk.text), matched, score))
        results.sort(key=lambda note: (-note.score, note.category, note.source, note.title))
        return [note.to_dict() for note in results[: max(0, int(limit))]]

    def _load_chunks(self) -> list[_Chunk]:
        if self._chunks is not None:
            return self._chunks
        chunks: list[_Chunk] = []
        if not self.available:
            self._chunks = []
            return chunks
        paths: list[Path] = []
        for category in _CATEGORIES:
            category_root = self.root / category
            if category_root.is_dir():
                paths.extend(path for path in sorted(category_root.rglob("*.md")) if path.is_file())
        for path in paths[: self.max_files]:
            relative = path.relative_to(self.root)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            chunks.extend(self._split(relative.parts[0], path, text))
        self._chunks = chunks
        return chunks

    def _split(self, category: str, source: Path, text: str) -> list[_Chunk]:
        sections: list[_Chunk] = []
        title = source.stem
        buffer: list[str] = []
        for line in text.splitlines():
            match = _HEADING_RE.match(line)
            if match:
                if buffer and "\n".join(buffer).strip():
                    sections.append(_Chunk(category, title, source, "\n".join(buffer).strip()))
                title = match.group(1).strip()
                buffer = []
            else:
                buffer.append(line)
        if buffer and "\n".join(buffer).strip():
            sections.append(_Chunk(category, title, source, "\n".join(buffer).strip()))
        return sections

    def _snippet(self, text: str) -> str:
        compact = re.sub(r"\s+", " ", text).strip()
        if len(compact) <= self.snippet_limit:
            return compact
        return compact[: self.snippet_limit].rsplit(" ", 1)[0] + "..."

    @staticmethod
    def _terms(value: str | Iterable[str]) -> set[str]:
        text = value if isinstance(value, str) else " ".join(str(item) for item in value)
        lowered = text.lower()
        for phrase, aliases in _QUERY_ALIASES.items():
            if phrase in lowered:
                text += " " + " ".join(aliases)
        return {token.lower() for token in _TOKEN_RE.findall(text) if len(token) >= 2}
