from __future__ import annotations

import re
from pathlib import Path


TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


def render_template(template: str, values: dict[str, object]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            if key in {"relevant_skill_notes_json", "structured_observations_json"}:
                return "[]"
            raise KeyError(f"Missing prompt variable: {key}")
        return str(values[key])

    return TOKEN_RE.sub(replace, template)

class PromptStore:
    def __init__(self, prompt_dir: str | Path | None = None) -> None:
        self.prompt_dir = Path(prompt_dir).expanduser() if prompt_dir else default_prompt_dir()

    def load(self, name: str) -> str:
        path = self.prompt_dir / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"Prompt not found: {path}")
        return path.read_text(encoding="utf-8")

    def render(self, name: str, values: dict[str, object]) -> str:
        return render_template(self.load(name), values)


def default_prompt_dir() -> Path:
    cwd_prompts = Path.cwd() / "prompts"
    if cwd_prompts.is_dir():
        return cwd_prompts
    return Path(__file__).resolve().parents[3] / "prompts"
