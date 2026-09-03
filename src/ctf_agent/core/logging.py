from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ctf_agent.core.config import get_nested


def setup_logging(config: dict[str, Any]) -> None:
    level_name = str(get_nested(config, ("logging", "level")) or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )


class JsonlTraceWriter:
    def __init__(self, path: str | Path, enabled: bool = True) -> None:
        self.path = Path(path).expanduser()
        self.enabled = enabled

    def emit(self, event_type: str, **payload: Any) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "type": event_type,
            "payload": payload,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def trace_from_config(config: dict[str, Any]) -> JsonlTraceWriter:
    enabled = bool(get_nested(config, ("logging", "trace_enabled")))
    path = str(get_nested(config, ("logging", "trace_path")) or "~/ctf-workspace/traces/ctf-agent.jsonl")
    return JsonlTraceWriter(path=path, enabled=enabled)
