from __future__ import annotations

import html
import re
from typing import Any

from ctf_agent.analysis.php import analyze_php_text, extract_php_sources


class ObservationSummarizer:
    """Turn command/HTTP output into bounded, machine-readable evidence."""

    def __init__(self, *, body_limit: int = 1800, source_limit: int = 12000) -> None:
        self.body_limit = max(200, body_limit)
        self.source_limit = max(1000, source_limit)

    def summarize(self, text: str, *, timed_out: bool = False) -> dict[str, Any]:
        raw = text or ""
        evidence: dict[str, Any] = {
            "status": self._status(raw),
            "headers": self._headers(raw),
            "title": self._first(raw, r"<title[^>]*>(.*?)</title>"),
            "forms": self._forms(raw),
            "links": self._attributes(raw, "a", "href"),
            "scripts": self._attributes(raw, "script", "src"),
            "body_excerpt": self._body_excerpt(raw),
            "timed_out": bool(timed_out),
        }
        sources = extract_php_sources(raw)
        if sources:
            source = "\n\n".join(sources)[: self.source_limit]
            analysis = analyze_php_text(source).to_dict()
            evidence["php_source"] = source
            evidence["php_analysis"] = {
                "parameters": analysis["parameters"],
                "sinks": sorted(set(analysis["dangerous_functions"] + [item["function"] for item in analysis["include_points"]])),
                "guards": analysis["comparisons"],
                "blacklist": analysis["blacklist_patterns"],
                "candidate_strategies": analysis["strategies"],
            }
        return evidence

    def _status(self, text: str) -> int | None:
        match = re.search(r"HTTP/(?:1\.[01]|2)\s+(\d{3})\b", text, re.I)
        return int(match.group(1)) if match else None

    def _headers(self, text: str) -> dict[str, str]:
        match = re.search(r"(?:^|\n)HTTP/[^\n]+\n(.*?)(?:\n\s*\n|\Z)", text, re.I | re.S)
        if not match:
            return {}
        headers: dict[str, str] = {}
        for line in match.group(1).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()[:300]
        return headers

    def _forms(self, text: str) -> list[dict[str, Any]]:
        forms: list[dict[str, Any]] = []
        for match in re.finditer(r"<form\b([^>]*)>(.*?)</form\s*>", text, re.I | re.S):
            attrs = self._attr_map(match.group(1))
            inputs = []
            for tag in re.findall(r"<(?:input|textarea|select)\b[^>]*>", match.group(2), re.I):
                item = self._attr_map(tag)
                if item.get("name"):
                    inputs.append({key: item[key] for key in ("name", "type", "value") if key in item})
            forms.append({"action": attrs.get("action", ""), "method": attrs.get("method", "get").lower(), "inputs": inputs})
        return forms[:30]

    def _attributes(self, text: str, tag: str, attr: str) -> list[str]:
        values = []
        for match in re.finditer(rf"<{tag}\b([^>]*)>", text, re.I | re.S):
            value = self._attr_map(match.group(1)).get(attr.lower())
            if value:
                values.append(value.strip()[:500])
        return list(dict.fromkeys(values))[:50]

    def _body_excerpt(self, text: str) -> str:
        body = re.sub(r"<script\b.*?</script\s*>", " ", text, flags=re.I | re.S)
        body = re.sub(r"<style\b.*?</style\s*>", " ", body, flags=re.I | re.S)
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", html.unescape(body)).strip()
        return body[: self.body_limit]

    @staticmethod
    def _first(text: str, pattern: str) -> str:
        match = re.search(pattern, text, re.I | re.S)
        return re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()[:300] if match else ""

    @staticmethod
    def _attr_map(text: str) -> dict[str, str]:
        result: dict[str, str] = {}
        pattern = r'''([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))'''
        for match in re.finditer(pattern, text, re.S):
            result[match.group(1).lower()] = html.unescape(next(value for value in match.groups()[1:] if value is not None))
        return result
