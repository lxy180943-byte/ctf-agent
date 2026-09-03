from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from ctf_agent.tools.registry import ToolRegistry
from ctf_agent.tools.spec import ToolSpec


@dataclass
class ToolCheck:
    tool: ToolSpec
    available: bool
    missing_bins: list[str]
    resolved_bins: dict[str, str]
    missing_python_packages: list[str]
    resolved_python_packages: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool.to_dict(),
            "available": self.available,
            "missing_bins": list(self.missing_bins),
            "resolved_bins": dict(self.resolved_bins),
            "missing_python_packages": list(self.missing_python_packages),
            "resolved_python_packages": list(self.resolved_python_packages),
            "install_hint": self.tool.install_hint,
        }


def check_tool(tool: ToolSpec) -> ToolCheck:
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for binary in tool.required_bins:
        path = _resolve_binary(binary)
        if path:
            resolved[binary] = path
        else:
            missing.append(binary)
    python_package = tool.metadata.get("python_package")
    missing_packages: list[str] = []
    resolved_packages: list[str] = []
    if isinstance(python_package, str) and python_package:
        if _python_package_available(python_package):
            resolved_packages.append(python_package)
        else:
            missing_packages.append(python_package)
    return ToolCheck(
        tool=tool,
        available=not missing and not missing_packages,
        missing_bins=missing,
        resolved_bins=resolved,
        missing_python_packages=missing_packages,
        resolved_python_packages=resolved_packages,
    )


def _resolve_binary(binary: str) -> str | None:
    path = shutil.which(binary)
    if path:
        return path
    if binary in {"python", "python3"}:
        executable = sys.executable
        if executable:
            return executable
    return None


def _python_package_available(package: str) -> bool:
    completed = subprocess.run(
        [sys.executable, "-c", f"import {package}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def build_tools_doctor(registry: ToolRegistry, category: str | None = None) -> dict[str, Any]:
    checks = [check_tool(tool) for tool in registry.list(category=category)]
    available = sum(1 for check in checks if check.available)
    missing = len(checks) - available
    return {
        "ok": True,
        "category": category,
        "total": len(checks),
        "available": available,
        "missing": missing,
        "checks": [check.to_dict() for check in checks],
    }


def print_tools_doctor(report: dict[str, Any]) -> None:
    print("CTF Agent Tools Doctor")
    print(f"OK: {report['ok']} available={report['available']} missing={report['missing']} total={report['total']}")
    for item in report["checks"]:
        tool = item["tool"]
        status = "ok" if item["available"] else "missing"
        missing = ",".join(item["missing_bins"]) if item["missing_bins"] else "-"
        missing_packages = ",".join(item["missing_python_packages"]) if item["missing_python_packages"] else "-"
        print(
            f"- {tool['category']}/{tool['name']}: {status} "
            f"required={','.join(tool['required_bins']) or '-'} missing={missing} missing_packages={missing_packages}"
        )
        if not item["available"] and item["install_hint"]:
            print(f"  install: {item['install_hint']}")
