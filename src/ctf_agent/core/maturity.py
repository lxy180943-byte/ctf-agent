from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from ctf_agent.core.config import get_nested, project_root
from ctf_agent.core.doctor import check_llm_config
from ctf_agent.core.models import utc_now
from ctf_agent.memory import MemoryStore
from ctf_agent.sandbox.images import docker_profiles_doctor
from ctf_agent.tools import build_tools_doctor, default_registry
from ctf_agent.ui.server import ThreadedWorkbenchServer, make_server

MUTURITY_LEVELS = [
    "scaffold",
    "workflow-ready",
    "competition-assistant",
    "autonomous-baseline",
    "mature",
]

DEFAULT_MATURITY_REPORT_PATH = project_root() / "docs" / "maturity_report.md"


@dataclass
class MaturityReport:
    generated_at: str
    level: str
    levels: list[str]
    llm: dict[str, Any]
    tools: dict[str, Any]
    docker: dict[str, Any]
    benchmark: dict[str, Any]
    memory: dict[str, Any]
    ui: dict[str, Any]
    safety: dict[str, Any]
    missing_to_mature: list[str]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "level": self.level,
            "levels": list(self.levels),
            "llm": dict(self.llm),
            "tools": dict(self.tools),
            "docker": dict(self.docker),
            "benchmark": dict(self.benchmark),
            "memory": dict(self.memory),
            "ui": dict(self.ui),
            "safety": dict(self.safety),
            "missing_to_mature": list(self.missing_to_mature),
            "notes": list(self.notes),
        }


def build_maturity_report(
    config: dict[str, Any],
    *,
    eval_summary: Any | None = None,
    eval_summary_path: str | Path | None = None,
) -> dict[str, Any]:
    llm = _llm_section(config)
    tools = _tools_section()
    docker = _docker_section()
    benchmark = _benchmark_section(config, eval_summary=eval_summary, eval_summary_path=eval_summary_path)
    memory = _memory_section(config)
    ui = _ui_section(config)
    safety = _safety_section(config, llm)

    workflow_ready = bool(ui["ok"] and safety["ok"] and tools["available_ratio"] >= 0.5)
    competition_ready = bool(
        workflow_ready
        and llm["real_provider"]
        and tools["available_ratio"] >= 0.8
        and docker["ok"]
    )
    autonomous_ready = bool(
        competition_ready
        and benchmark["pass_rate"] is not None
        and benchmark["pass_rate"] >= 0.6
        and memory["ok"]
    )
    mature = bool(
        autonomous_ready
        and benchmark["pass_rate"] is not None
        and benchmark["pass_rate"] >= 0.85
        and benchmark["false_positive_rate"] == 0
        and tools["available_ratio"] >= 0.9
        and memory["quality_score"] >= 0.75
    )

    if mature:
        level = "mature"
    elif autonomous_ready:
        level = "autonomous-baseline"
    elif competition_ready:
        level = "competition-assistant"
    elif workflow_ready:
        level = "workflow-ready"
    else:
        level = "scaffold"

    report = MaturityReport(
        generated_at=utc_now(),
        level=level,
        levels=list(MUTURITY_LEVELS),
        llm=llm,
        tools=tools,
        docker=docker,
        benchmark=benchmark,
        memory=memory,
        ui=ui,
        safety=safety,
        missing_to_mature=_missing_to_mature(llm, tools, docker, benchmark, memory, ui, safety),
        notes=_notes_for_report(llm, tools, docker, benchmark, memory, ui, safety, level),
    )
    return report.to_dict()


def render_maturity_report(report: dict[str, Any]) -> str:
    lines = [
        "# CTF Agent Maturity Report",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- level: `{report['level']}`",
        f"- levels: `{', '.join(report['levels'])}`",
        "",
        "## LLM",
        "",
    ]
    llm = report["llm"]
    lines.extend(
        [
            f"- provider: `{llm['provider']}`",
            f"- model: `{llm['model']}`",
            f"- base_url: `{llm['base_url']}`",
            f"- timeout_seconds: `{llm['timeout_seconds']}`",
            f"- api_key_present: `{llm['api_key_present']}`",
            f"- real_provider: `{llm['real_provider']}`",
            f"- ok: `{llm['ok']}`",
        ]
    )
    if llm.get("error"):
        lines.append(f"- error: `{llm['error']}`")
    if llm.get("recommendation"):
        lines.append(f"- recommendation: `{llm['recommendation']}`")

    tools = report["tools"]
    lines.extend(["", "## Tools", ""])
    lines.extend(
        [
            f"- total: `{tools['total']}`",
            f"- available: `{tools['available']}`",
            f"- available_ratio: `{tools['available_ratio']:.2f}`",
            f"- missing: `{tools['missing']}`",
        ]
    )
    if tools.get("missing_tools"):
        lines.append(f"- missing_tools: `{', '.join(tools['missing_tools'])}`")
    if tools.get("notes"):
        lines.extend(f"- note: `{note}`" for note in tools["notes"])

    docker = report["docker"]
    lines.extend(["", "## Docker", ""])
    lines.extend(
        [
            f"- docker_available: `{docker['docker_available']}`",
            f"- ok: `{docker['ok']}`",
            f"- ready_profiles: `{', '.join(docker['ready_profiles']) or '-'}`",
            f"- missing_profiles: `{', '.join(docker['missing_profiles']) or '-'}`",
        ]
    )

    benchmark = report["benchmark"]
    lines.extend(["", "## Benchmark", ""])
    lines.extend(
        [
            f"- summary_path: `{benchmark['summary_path'] or '-'}`",
            f"- dataset: `{benchmark['dataset'] or '-'}`",
            f"- challenge_count: `{benchmark['challenge_count']}`",
            f"- solved_count: `{benchmark['solved_count']}`",
            f"- pass_rate: `{benchmark['pass_rate'] if benchmark['pass_rate'] is not None else '-'}`",
            f"- false_positive_rate: `{benchmark['false_positive_rate']}`",
            f"- verifier_false_positive: `{benchmark['verifier_false_positive']}`",
        ]
    )
    if benchmark.get("capability_gaps"):
        gaps = benchmark["capability_gaps"]
        lines.append(f"- weak_categories: `{', '.join(gaps.get('weak_categories', [])) or '-'}`")

    memory = report["memory"]
    lines.extend(["", "## Memory", ""])
    lines.extend(
        [
            f"- enabled: `{memory['enabled']}`",
            f"- ok: `{memory['ok']}`",
            f"- total_items: `{memory['total_items']}`",
            f"- traceable_ratio: `{memory['traceable_ratio']:.2f}`",
            f"- avg_confidence: `{memory['avg_confidence']:.2f}`",
            f"- quality_score: `{memory['quality_score']:.2f}`",
        ]
    )
    if memory.get("notes"):
        lines.extend(f"- note: `{note}`" for note in memory["notes"])

    ui = report["ui"]
    lines.extend(["", "## UI", ""])
    lines.extend(
        [
            f"- ok: `{ui['ok']}`",
            f"- health_url: `{ui['health_url'] or '-'}`",
        ]
    )
    if ui.get("error"):
        lines.append(f"- error: `{ui['error']}`")

    safety = report["safety"]
    lines.extend(["", "## Safety", ""])
    lines.extend(
        [
            f"- ok: `{safety['ok']}`",
            f"- dry_run_default: `{safety['dry_run_default']}`",
            f"- allow_network_default: `{safety['allow_network_default']}`",
            f"- llm_env_only: `{safety['llm_env_only']}`",
            f"- trace_redaction: `{safety['trace_redaction']}`",
        ]
    )
    if safety.get("notes"):
        lines.extend(f"- note: `{note}`" for note in safety["notes"])

    lines.extend(["", "## Missing To Mature", ""])
    if report["missing_to_mature"]:
        lines.extend(f"- {item}" for item in report["missing_to_mature"])
    else:
        lines.append("- none")

    lines.extend(["", "## Notes", ""])
    if report["notes"]:
        lines.extend(f"- {item}" for item in report["notes"])
    else:
        lines.append("- none")

    return "\n".join(lines) + "\n"


def write_maturity_report(
    config: dict[str, Any],
    *,
    eval_summary: Any | None = None,
    eval_summary_path: str | Path | None = None,
    output_path: str | Path | None = None,
) -> Path:
    report = build_maturity_report(config, eval_summary=eval_summary, eval_summary_path=eval_summary_path)
    output = Path(output_path).expanduser() if output_path else DEFAULT_MATURITY_REPORT_PATH
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_maturity_report(report), encoding="utf-8")
    return output


def latest_eval_summary_path(config: dict[str, Any], explicit_path: str | Path | None = None) -> Path | None:
    if explicit_path:
        path = Path(explicit_path).expanduser()
        return path if path.exists() else None

    candidates: list[Path] = []
    workspace = Path(get_nested(config, ("workspace_dir",)) or "~/ctf-workspace").expanduser()
    candidates.extend(_collect_summary_files(workspace / "evals"))
    candidates.extend(_collect_summary_files(project_root() / "evals"))
    candidates.extend(_collect_summary_files(Path.cwd() / "evals"))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item.stat().st_mtime, str(item)))


def load_eval_summary(config: dict[str, Any], explicit_path: str | Path | None = None) -> tuple[Path | None, dict[str, Any] | None]:
    path = latest_eval_summary_path(config, explicit_path=explicit_path)
    if path is None:
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return path, None
    if not isinstance(data, dict):
        return path, None
    return path, data


def _llm_section(config: dict[str, Any]) -> dict[str, Any]:
    status = check_llm_config(config)
    provider = status.provider
    real_provider = provider in {"openai", "openai-compatible"} and status.api_key_present and bool(status.model) and bool(status.base_url)
    return {
        "provider": provider,
        "model": status.model,
        "base_url": status.base_url,
        "timeout_seconds": status.timeout_seconds,
        "api_key_present": status.api_key_present,
        "real_provider": real_provider,
        "ok": status.ok,
        "error": status.error,
        "recommendation": status.recommendation,
    }


def _tools_section() -> dict[str, Any]:
    report = build_tools_doctor(default_registry())
    checks = report.get("checks", [])
    missing_tools = []
    notes = []
    for item in checks:
        if not isinstance(item, dict):
            continue
        tool = item.get("tool", {})
        if not item.get("available") and isinstance(tool, dict):
            missing_tools.append(f"{tool.get('category', '-')}/{tool.get('name', '-')}")
            hint = item.get("install_hint")
            if hint:
                notes.append(str(hint))
    total = int(report.get("total", 0) or 0)
    available = int(report.get("available", 0) or 0)
    return {
        "ok": bool(report.get("ok", True)),
        "total": total,
        "available": available,
        "missing": max(0, total - available),
        "available_ratio": round(available / total, 6) if total else 0.0,
        "missing_tools": missing_tools,
        "notes": _dedupe(notes),
        "checks": checks,
    }


def _docker_section() -> dict[str, Any]:
    report = docker_profiles_doctor(run_tools=False, include_optional=False)
    profiles = report.get("profiles", [])
    ready_profiles = []
    missing_profiles = []
    for item in profiles:
        if not isinstance(item, dict):
            continue
        name = str(item.get("profile") or "")
        if item.get("ok"):
            ready_profiles.append(name)
        elif name:
            missing_profiles.append(name)
    return {
        "docker_available": bool(report.get("docker_available")),
        "ok": bool(report.get("ok")),
        "ready_profiles": ready_profiles,
        "missing_profiles": missing_profiles,
        "profiles": profiles,
    }


def _benchmark_section(
    config: dict[str, Any],
    *,
    eval_summary: Any | None = None,
    eval_summary_path: str | Path | None = None,
) -> dict[str, Any]:
    source_path: Path | None = None
    data: dict[str, Any] | None = None
    if eval_summary is not None:
        if hasattr(eval_summary, "to_dict"):
            data = eval_summary.to_dict()
            source_path = Path(getattr(eval_summary, "output_dir", "")) / "eval_summary.json" if getattr(eval_summary, "output_dir", None) else None
        elif isinstance(eval_summary, dict):
            data = dict(eval_summary)
            output_dir = data.get("output_dir")
            source_path = Path(output_dir) / "eval_summary.json" if output_dir else None
    if data is None:
        source_path, data = load_eval_summary(config, explicit_path=eval_summary_path)
    metrics = dict(data.get("metrics", {})) if isinstance(data, dict) else {}
    challenge_count = int(metrics.get("challenge_count", 0) or 0)
    solved_count = int(metrics.get("solved_count", 0) or 0)
    verifier_false_positive = int(metrics.get("verifier_false_positive", 0) or 0)
    pass_rate = round(solved_count / challenge_count, 6) if challenge_count else None
    false_positive_rate = round(verifier_false_positive / challenge_count, 6) if challenge_count else 0.0
    capability_gaps = data.get("capability_gaps", {}) if isinstance(data, dict) else {}
    dataset = data.get("dataset") if isinstance(data, dict) else None
    return {
        "summary_path": str(source_path) if source_path else None,
        "dataset": dataset,
        "challenge_count": challenge_count,
        "solved_count": solved_count,
        "pass_rate": pass_rate,
        "false_positive_rate": false_positive_rate,
        "verifier_false_positive": verifier_false_positive,
        "metrics": metrics,
        "capability_gaps": capability_gaps,
    }


def _memory_section(config: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(get_nested(config, ("memory", "enabled")))
    notes = []
    try:
        store = MemoryStore.from_config(config)
        items = store.list(limit=500)
    except Exception as exc:
        return {
            "enabled": enabled,
            "ok": False,
            "total_items": 0,
            "traceable_ratio": 0.0,
            "avg_confidence": 0.0,
            "quality_score": 0.0,
            "notes": [str(exc)],
        }

    total = len(items)
    traceable = sum(1 for item in items if str(item.source_run).strip())
    confidences = [float(item.confidence) for item in items]
    avg_confidence = round(mean(confidences), 6) if confidences else 0.0
    low_confidence = sum(1 for item in items if float(item.confidence) < 0.35)
    failure_heavy = sum(1 for item in items if int(item.failure_count) > int(item.success_count))
    traceable_ratio = round(traceable / total, 6) if total else 0.0
    low_confidence_ratio = low_confidence / total if total else 1.0
    failure_ratio = failure_heavy / total if total else 1.0
    quality_score = round(max(0.0, 1.0 - (1.0 - traceable_ratio) * 0.5 - low_confidence_ratio * 0.3 - failure_ratio * 0.2), 6) if total else 0.0
    if not enabled:
        notes.append("memory is disabled in config")
    if total == 0:
        notes.append("no memory items recorded yet")
    elif traceable_ratio < 0.9:
        notes.append("some memory items lack traceable source_run metadata")
    if avg_confidence < 0.55 and total:
        notes.append("average memory confidence is still low")
    if failure_ratio > 0.4 and total:
        notes.append("too many memory items look failure-heavy")
    return {
        "enabled": enabled,
        "ok": bool(enabled and total > 0 and traceable_ratio >= 0.9 and avg_confidence >= 0.55 and quality_score >= 0.6),
        "total_items": total,
        "traceable_ratio": traceable_ratio,
        "avg_confidence": avg_confidence,
        "quality_score": quality_score,
        "notes": _dedupe(notes),
    }


def _ui_section(config: dict[str, Any]) -> dict[str, Any]:
    try:
        httpd = make_server(config, host="127.0.0.1", port=0)
    except Exception as exc:
        return {"ok": False, "health_url": None, "error": str(exc)}
    server = ThreadedWorkbenchServer(httpd).start()
    health_url = server.url + "/api/health"
    try:
        with urllib.request.urlopen(health_url, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        ok = bool(payload.get("ok"))
        return {"ok": ok, "health_url": health_url, "payload": payload}
    except Exception as exc:
        return {"ok": False, "health_url": health_url, "error": str(exc)}
    finally:
        server.stop()


def _safety_section(config: dict[str, Any], llm: dict[str, Any]) -> dict[str, Any]:
    dry_run_default = get_nested(config, ("submit", "dry_run_default"))
    allow_network_default = get_nested(config, ("sandbox", "allow_network"))
    llm_config = config.get("llm") if isinstance(config.get("llm"), dict) else {}
    llm_env_only = all(key not in llm_config for key in ("api_key", "base_url", "model"))
    trace_redaction = True
    notes = []
    if dry_run_default is not True:
        notes.append("submit.dry_run_default should stay true by default")
    if allow_network_default:
        notes.append("sandbox.allow_network defaults to true")
    if not llm_env_only:
        notes.append("llm config still contains environment-only fields")
    ok = bool(dry_run_default is True and not allow_network_default and llm_env_only and trace_redaction)
    return {
        "ok": ok,
        "dry_run_default": bool(dry_run_default is True),
        "allow_network_default": bool(allow_network_default),
        "llm_env_only": llm_env_only,
        "trace_redaction": trace_redaction,
        "notes": _dedupe(notes),
    }


def _missing_to_mature(
    llm: dict[str, Any],
    tools: dict[str, Any],
    docker: dict[str, Any],
    benchmark: dict[str, Any],
    memory: dict[str, Any],
    ui: dict[str, Any],
    safety: dict[str, Any],
) -> list[str]:
    items: list[str] = []
    if not llm["real_provider"]:
        items.append("Configure OPENAI_API_KEY, OPENAI_BASE_URL, and OPENAI_MODEL for a real GPT/Codex provider.")
    if tools["available_ratio"] < 0.9:
        items.append(f"Raise tool availability from {tools['available_ratio']:.2f} to at least 0.90.")
    if not docker["ok"]:
        items.append("Build or repair the missing Docker sandbox images.")
    if not ui["ok"]:
        items.append("Fix the local UI server health path.")
    if not safety["ok"]:
        items.append("Keep dry-run default on, network default off, and environment-only LLM config.")
    if benchmark["pass_rate"] is None:
        items.append("Run the local benchmark suite and record a pass rate.")
    elif benchmark["pass_rate"] < 0.85:
        items.append(f"Increase benchmark pass rate from {benchmark['pass_rate']:.2f} to at least 0.85.")
    if benchmark["verifier_false_positive"]:
        items.append("Eliminate verifier false positives in benchmark runs.")
    if not memory["ok"]:
        items.append("Improve memory quality and traceability.")
    return _dedupe(items)


def _notes_for_report(
    llm: dict[str, Any],
    tools: dict[str, Any],
    docker: dict[str, Any],
    benchmark: dict[str, Any],
    memory: dict[str, Any],
    ui: dict[str, Any],
    safety: dict[str, Any],
    level: str,
) -> list[str]:
    notes = []
    if level != "mature":
        notes.append(f"Current level is {level}; the report is intentionally conservative.")
    if benchmark["capability_gaps"]:
        weak = benchmark["capability_gaps"].get("weak_categories", [])
        if weak:
            notes.append("Benchmark weak categories: " + ", ".join(weak))
    if memory["total_items"] == 0:
        notes.append("Memory will look better once real solved/failed runs are learned back into the store.")
    if llm["provider"] in {"none", "disabled", "off", "dry-run", "fallback"}:
        notes.append("No real LLM provider is configured yet.")
    if not docker["docker_available"]:
        notes.append("Docker is not available in this environment.")
    if ui["ok"]:
        notes.append("UI health endpoint is reachable locally.")
    if safety["ok"]:
        notes.append("Dry-run submission and redaction policy are in the safe default state.")
    return _dedupe(notes)


def _collect_summary_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [path for path in root.rglob("eval_summary.json") if path.is_file()]


def _dedupe(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen
