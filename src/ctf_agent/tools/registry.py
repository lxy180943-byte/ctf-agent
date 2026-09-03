from __future__ import annotations

from collections import defaultdict

from ctf_agent.tools.spec import ToolSpec


class ToolRegistry:
    def __init__(self, tools: list[ToolSpec] | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        if tools:
            for tool in tools:
                self.register(tool)

    def register(self, tool: ToolSpec) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Tool not found: {name}") from exc

    def list(self, category: str | None = None) -> list[ToolSpec]:
        tools = list(self._tools.values())
        if category is not None:
            tools = [tool for tool in tools if tool.category == category]
        return sorted(tools, key=lambda item: (item.category, item.name))

    def categories(self) -> list[str]:
        return sorted({tool.category for tool in self._tools.values()})

    def query(self, text: str) -> list[ToolSpec]:
        needle = text.strip().lower()
        if not needle:
            return self.list()
        return [
            tool
            for tool in self.list()
            if needle in tool.name.lower() or needle in tool.category.lower() or needle in tool.description.lower()
        ]

    def recommend(self, category: str, limit: int | None = None) -> list[ToolSpec]:
        grouped: dict[str, list[ToolSpec]] = defaultdict(list)
        for tool in self.list():
            grouped[tool.category].append(tool)
        recommendations = grouped.get(category, []) or grouped.get("generic", [])
        return recommendations[:limit] if limit is not None else recommendations
