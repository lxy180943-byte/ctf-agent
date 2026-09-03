#!/usr/bin/env python3
from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECK_ROOTS = ("src", "tests", "scripts")


def iter_python_files() -> list[Path]:
    files: list[Path] = []
    for root_name in CHECK_ROOTS:
        root = ROOT / root_name
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if any(part in {"__pycache__", ".venv"} for part in path.parts):
                continue
            files.append(path)
    return sorted(files)


def check_file(path: Path) -> list[str]:
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")
    try:
        ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        errors.append(f"{path}:{exc.lineno}:{exc.offset}: syntax error: {exc.msg}")
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.rstrip() != line:
            errors.append(f"{path}:{line_number}: trailing whitespace")
        if "\t" in line[: len(line) - len(line.lstrip())]:
            errors.append(f"{path}:{line_number}: indentation uses tabs")
    if text and not text.endswith("\n"):
        errors.append(f"{path}: missing final newline")
    return errors


def main() -> int:
    errors: list[str] = []
    for path in iter_python_files():
        errors.extend(check_file(path))
    if errors:
        print("Basic lint failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Basic lint OK: {len(iter_python_files())} Python files checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
