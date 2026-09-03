from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    category: str
    description: str
    command_template: str
    inputs: list[str] = field(default_factory=list)
    risk_level: RiskLevel | str = RiskLevel.LOW
    required_bins: list[str] = field(default_factory=list)
    install_hint: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        risk = self.risk_level.value if isinstance(self.risk_level, RiskLevel) else str(self.risk_level)
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "command_template": self.command_template,
            "inputs": list(self.inputs),
            "risk_level": risk,
            "required_bins": list(self.required_bins),
            "install_hint": self.install_hint,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolSpec:
        return cls(
            name=str(data["name"]),
            category=str(data["category"]),
            description=str(data.get("description", "")),
            command_template=str(data.get("command_template", "")),
            inputs=[str(item) for item in data.get("inputs", [])],
            risk_level=RiskLevel(str(data.get("risk_level", RiskLevel.LOW.value))),
            required_bins=[str(item) for item in data.get("required_bins", [])],
            install_hint=str(data.get("install_hint", "")),
            metadata=dict(data.get("metadata", {})),
        )
