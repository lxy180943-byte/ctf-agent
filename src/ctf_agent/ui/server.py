from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ctf_agent.core.config import get_nested
from ctf_agent.core.models import FlagCandidate, Observation, Step, utc_now
from ctf_agent.core.orchestrator import Orchestrator
from ctf_agent.core.reporter import Reporter
from ctf_agent.core.state import ChallengeRunState
from ctf_agent.core.submitter import Submitter
from ctf_agent.core.trace import TraceEvent, TraceStore, summarize_text
from ctf_agent.core.workspace import WorkspaceManager, slugify
from ctf_agent.platforms.local import LocalPlatformAdapter


MAX_TEXT_BYTES = 1_000_000


@dataclass
class UIContext:
    config: dict[str, Any]
    workspace: WorkspaceManager
    challenge_root: Path
    artifacts_root: Path


class ThreadedWorkbenchServer:
    def __init__(self, httpd: ThreadingHTTPServer) -> None:
        self.httpd = httpd
        self.thread = threading.Thread(target=httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        host = "127.0.0.1" if host in {"", "0.0.0.0"} else str(host)
        return f"http://{host}:{port}"

    def start(self) -> ThreadedWorkbenchServer:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def serve(config: dict[str, Any], *, host: str = "127.0.0.1", port: int = 8008, challenge_root: str | Path | None = None) -> None:
    httpd = make_server(config, host=host, port=port, challenge_root=challenge_root)
    print(f"CTF Agent workbench: http://{host}:{httpd.server_address[1]}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping workbench.")
    finally:
        httpd.server_close()


def make_server(
    config: dict[str, Any],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    challenge_root: str | Path | None = None,
) -> ThreadingHTTPServer:
    handler = build_handler(config, challenge_root=challenge_root)
    return ThreadingHTTPServer((host, port), handler)


def build_handler(config: dict[str, Any], challenge_root: str | Path | None = None):
    workspace = WorkspaceManager(get_nested(config, ("workspace_dir",)) or "~/ctf-workspace")
    artifacts_root = Path(get_nested(config, ("artifacts_dir",)) or "~/ctf-artifacts").expanduser()
    root = Path(challenge_root or get_nested(config, ("ui", "challenge_root")) or "examples").expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    context = UIContext(config=config, workspace=workspace, challenge_root=root.resolve(), artifacts_root=artifacts_root)

    class WorkbenchHandler(BaseHTTPRequestHandler):
        server_version = "CTFAgentWorkbench/0.2"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    self._send_html(WORKBENCH_HTML)
                elif parsed.path == "/assets/app.css":
                    self._send_text(APP_CSS, "text/css; charset=utf-8")
                elif parsed.path == "/assets/app.js":
                    self._send_text(APP_JS, "application/javascript; charset=utf-8")
                elif parsed.path == "/api/health":
                    self._send_json(
                        {
                            "ok": True,
                            "workspace": str(context.workspace.workspace_root),
                            "challenge_root": str(context.challenge_root),
                            "artifacts_root": str(context.artifacts_root),
                            "windows_artifacts_root": windows_path_hint(context.artifacts_root),
                        }
                    )
                elif parsed.path == "/api/challenges":
                    self._send_json({"challenges": list_challenges(context, parse_qs(parsed.query))})
                elif parsed.path == "/api/runs":
                    self._send_json({"runs": list_runs(context)})
                elif parsed.path.startswith("/api/runs/"):
                    self._handle_run_get(context, parsed.path, parse_qs(parsed.query))
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "not found")
            except Exception as exc:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path.startswith("/api/challenges/") and parsed.path.endswith("/solve"):
                    challenge_id = unquote(parsed.path.removeprefix("/api/challenges/").removesuffix("/solve").strip("/"))
                    self._send_json(solve_challenge(context, challenge_id, self._read_json()))
                elif parsed.path.startswith("/api/runs/"):
                    self._handle_run_post(context, parsed.path, self._read_json())
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "not found")
            except Exception as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

        def _handle_run_get(self, context: UIContext, path: str, query: dict[str, list[str]]) -> None:
            rest = unquote(path.removeprefix("/api/runs/"))
            parts = [part for part in rest.split("/") if part]
            if not parts:
                self._send_error(HTTPStatus.NOT_FOUND, "missing run id")
                return
            run_id = parts[0]
            section = parts[1] if len(parts) > 1 else "state"
            if section == "state":
                self._send_json(get_run_state(context, run_id))
            elif section == "trace":
                self._send_json({"events": get_run_trace(context, run_id)})
            elif section == "files":
                self._send_json({"files": list_run_files(context, run_id, query)})
            elif section == "file":
                self._send_json(read_run_file(context, run_id, query))
            elif section == "writeup":
                self._send_json(read_writeup(context, run_id, generate=query.get("generate", ["false"])[0] == "true"))
            elif section == "notes":
                self._send_json(read_notes(context, run_id))
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "unknown run section")

        def _handle_run_post(self, context: UIContext, path: str, payload: dict[str, Any]) -> None:
            rest = unquote(path.removeprefix("/api/runs/"))
            parts = [part for part in rest.split("/") if part]
            if len(parts) < 2:
                self._send_error(HTTPStatus.NOT_FOUND, "missing run action")
                return
            run_id, action = parts[0], parts[1]
            if action == "submit":
                self._send_json(submit_run(context, run_id, payload))
            elif action == "resume":
                self._send_json(resume_run(context, run_id, payload))
            elif action == "report":
                self._send_json(report_run(context, run_id))
            elif action == "export":
                self._send_json(export_artifact(context, run_id, payload))
            elif action == "observation":
                self._send_json(add_manual_observation(context, run_id, payload))
            elif action == "flag":
                self._send_json(add_manual_flag(context, run_id, payload))
            elif action == "notes":
                self._send_json(write_notes(context, run_id, payload))
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "unknown run action")

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data

        def _send_html(self, html: str) -> None:
            self._send_text(html, "text/html; charset=utf-8")

        def _send_text(self, text: str, content_type: str) -> None:
            body = text.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, data: Any) -> None:
            body = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            body = json.dumps({"error": message, "status": int(status)}, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return WorkbenchHandler


def list_challenges(context: UIContext, query: dict[str, list[str]]) -> list[dict[str, Any]]:
    root = _challenge_root(context, query)
    adapter = LocalPlatformAdapter(root)
    runs_by_challenge = {run["challenge_id"]: run for run in list_runs(context)}
    rows: list[dict[str, Any]] = []
    for challenge in adapter.list_challenges():
        run = runs_by_challenge.get(challenge.id)
        data = challenge.to_dict()
        data["run_state"] = run["state"] if run else "new"
        data["run_id"] = run["id"] if run else slugify(challenge.id)
        data["solved"] = bool(run and run["state"] == "solved")
        rows.append(data)
    category = _query_one(query, "category")
    status = _query_one(query, "status")
    search = (_query_one(query, "search") or "").lower()
    solved = _query_one(query, "solved")
    if category and category != "all":
        rows = [row for row in rows if row.get("category") == category]
    if status and status != "all":
        rows = [row for row in rows if row.get("run_state") == status]
    if solved in {"true", "false"}:
        want = solved == "true"
        rows = [row for row in rows if bool(row.get("solved")) is want]
    if search:
        rows = [row for row in rows if search in _challenge_search_text(row)]
    return rows


def list_runs(context: UIContext) -> list[dict[str, Any]]:
    runs_root = context.workspace.runs_root
    if not runs_root.exists():
        return []
    runs: list[dict[str, Any]] = []
    for child in sorted(runs_root.iterdir(), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True):
        if not child.is_dir() or not (child / "state.json").exists():
            continue
        try:
            state = context.workspace.load_state(child.name)
        except Exception:
            continue
        runs.append(_state_summary(state, child))
    return runs


def get_run_state(context: UIContext, run_id: str) -> dict[str, Any]:
    run_dir = _run_dir(context, run_id)
    state = context.workspace.load_state(run_dir.name)
    events = TraceStore(run_dir / "trace.jsonl").read_events()
    summary = _state_summary(state, run_dir, events)
    return {"run": summary, "state": state.to_dict(), "notes": read_notes(context, run_id)["text"]}


def get_run_trace(context: UIContext, run_id: str) -> list[dict[str, Any]]:
    run_dir = _run_dir(context, run_id)
    return [event.to_dict() for event in TraceStore(run_dir / "trace.jsonl").read_events()]


def list_run_files(context: UIContext, run_id: str, query: dict[str, list[str]]) -> list[dict[str, Any]]:
    run_dir = _run_dir(context, run_id)
    area = query.get("area", ["all"])[0]
    roots = {
        "input": run_dir / "input",
        "work": run_dir / "work",
        "artifacts": run_dir / "artifacts",
        "all": run_dir,
    }
    root = roots.get(area, run_dir)
    if not root.exists():
        return []
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item)):
        if not path.is_file():
            continue
        stat = path.stat()
        rel = str(path.relative_to(run_dir))
        files.append(
            {
                "path": rel,
                "area": rel.split("/", 1)[0] if "/" in rel else area,
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "exportable": rel.startswith("artifacts/") or rel.startswith("work/"),
            }
        )
    return files


def read_run_file(context: UIContext, run_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
    run_dir = _run_dir(context, run_id)
    rel = query.get("path", [""])[0]
    if not rel:
        raise ValueError("path query is required")
    path = _safe_child(run_dir, rel)
    if not path.is_file():
        raise FileNotFoundError(f"file not found: {rel}")
    data = path.read_bytes()[:MAX_TEXT_BYTES]
    truncated = path.stat().st_size > len(data)
    return {
        "path": str(path.relative_to(run_dir)),
        "size": path.stat().st_size,
        "truncated": truncated,
        "text": data.decode("utf-8", errors="replace"),
        "content_type": mimetypes.guess_type(path.name)[0] or "text/plain",
    }


def read_writeup(context: UIContext, run_id: str, *, generate: bool = False) -> dict[str, Any]:
    run_dir = _run_dir(context, run_id)
    path = run_dir / "writeup.md"
    if generate or not path.exists():
        try:
            path = Reporter(context.workspace.workspace_root).generate(run_dir)
        except Exception:
            pass
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    return {"path": str(path), "exists": path.exists(), "text": text}


def solve_challenge(context: UIContext, challenge_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    adapter = LocalPlatformAdapter(context.challenge_root)
    challenge = adapter.get_challenge(challenge_id)
    orchestrator = Orchestrator(
        context.config,
        executor_name=str(payload.get("executor") or "local"),
        max_steps=int(payload.get("max_steps") or 20),
        timeout=int(payload.get("timeout") or get_nested(context.config, ("sandbox", "timeout_seconds")) or 60),
        brain=str(payload.get("brain") or "graph"),
        mode=str(payload.get("mode") or "single"),
    )
    result = orchestrator.solve(challenge, adapter=adapter)
    return _solve_result_dict(result)


def resume_run(context: UIContext, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, run_id)
    result = Orchestrator(
        context.config,
        executor_name=str(payload.get("executor") or "local"),
        max_steps=int(payload.get("max_steps") or 20),
        timeout=int(payload.get("timeout") or get_nested(context.config, ("sandbox", "timeout_seconds")) or 60),
        brain=str(payload.get("brain") or "graph"),
        mode=str(payload.get("mode") or "single"),
    ).resume_from_run_dir(run_dir)
    return _solve_result_dict(result)


def report_run(context: UIContext, run_id: str) -> dict[str, Any]:
    run_dir = _run_dir(context, run_id)
    path = Reporter(context.workspace.workspace_root).generate(run_dir)
    return {"writeup": str(path), "text": path.read_text(encoding="utf-8", errors="replace")}


def submit_run(context: UIContext, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, run_id)
    flag = payload.get("flag")
    submit = bool(payload.get("submit", False))
    if submit and payload.get("confirm") != "SUBMIT":
        raise ValueError("Real submission requires confirm=SUBMIT")
    result = Submitter(context.config).submit_run(run_dir, flag=flag if isinstance(flag, str) and flag else None, submit=submit)
    data = result.to_dict()
    data["dry_run"] = not submit or data.get("dry_run", True)
    return data


def export_artifact(context: UIContext, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, run_id)
    context.artifacts_root.mkdir(parents=True, exist_ok=True)
    export_dir = context.artifacts_root / run_id
    export_dir.mkdir(parents=True, exist_ok=True)
    rel = payload.get("path")
    exported: list[dict[str, Any]] = []
    if isinstance(rel, str) and rel:
        source = _safe_child(run_dir, rel)
        if not source.exists():
            raise FileNotFoundError(f"export source not found: {rel}")
        target = _copy_export(source, export_dir / source.name)
        exported.append({"source": str(source), "target": str(target), "windows_path": windows_path_hint(target)})
    else:
        for root in (run_dir / "artifacts",):
            if not root.exists():
                continue
            for source in root.rglob("*"):
                if source.is_file():
                    target = _copy_export(source, export_dir / source.relative_to(root))
                    exported.append({"source": str(source), "target": str(target), "windows_path": windows_path_hint(target)})
    return {"export_dir": str(export_dir), "windows_path": windows_path_hint(export_dir), "exported": exported}


def add_manual_observation(context: UIContext, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, run_id)
    state = context.workspace.load_state(run_dir.name)
    text = str(payload.get("text") or "").strip()
    if not text:
        raise ValueError("observation text is required")
    source = str(payload.get("source") or "manual")
    observation = Observation(summary=summarize_text(text, 800) or text, raw=text, source=source, metadata={"manual": True})
    attempt = state.attempts[-1] if state.attempts else state.start_attempt()
    attempt.add_step(Step(agent="human", action="manual-observation", observations=[observation], metadata={"manual": True, "source": source}))
    manual = state.metadata.setdefault("manual_observations", [])
    if isinstance(manual, list):
        manual.append(observation.to_dict())
    context.workspace.save_state(state)
    TraceStore(run_dir / "trace.jsonl").append(
        TraceEvent(challenge_id=state.challenge.id, agent="human", action="manual-observation", stdout=text, metadata={"source": source})
    )
    return {"observation": observation.to_dict(), "run": _state_summary(state, run_dir)}


def add_manual_flag(context: UIContext, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, run_id)
    state = context.workspace.load_state(run_dir.name)
    value = str(payload.get("flag") or "").strip()
    if not value:
        raise ValueError("flag is required")
    candidate = FlagCandidate(
        value=value,
        source=str(payload.get("source") or "manual"),
        confidence=float(payload.get("confidence") or 0.6),
        verified=bool(payload.get("verified", False)),
        submitted=False,
        metadata={"manual": True},
    )
    state.add_flag_candidate(candidate)
    context.workspace.save_state(state)
    TraceStore(run_dir / "trace.jsonl").append(
        TraceEvent(challenge_id=state.challenge.id, agent="human", action="manual-flag-candidate", stdout=value, metadata=candidate.to_dict())
    )
    return {"candidate": candidate.to_dict(), "run": _state_summary(state, run_dir)}


def read_notes(context: UIContext, run_id: str) -> dict[str, Any]:
    run_dir = _run_dir(context, run_id)
    path = run_dir / "artifacts" / "manual-notes.md"
    return {"path": str(path), "exists": path.exists(), "text": path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""}


def write_notes(context: UIContext, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, run_id)
    path = run_dir / "artifacts" / "manual-notes.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = str(payload.get("text") or "")
    path.write_text(text, encoding="utf-8")
    state = context.workspace.load_state(run_dir.name)
    state.metadata["manual_notes_path"] = str(path)
    context.workspace.save_state(state)
    TraceStore(run_dir / "trace.jsonl").append(TraceEvent(challenge_id=state.challenge.id, agent="human", action="manual-notes", stdout=summarize_text(text, 1000)))
    return {"path": str(path), "text": text}


def _state_summary(state: ChallengeRunState, run_dir: Path, events: list[TraceEvent] | None = None) -> dict[str, Any]:
    events = events if events is not None else []
    flags = [candidate.to_dict() for candidate in state.flag_candidates]
    latest_observation = _latest_observation(state, events)
    hypothesis = _current_hypothesis(events, state)
    failure_count = int(state.metadata.get("failure_count", 0) or 0)
    return {
        "id": run_dir.name,
        "path": str(run_dir),
        "challenge_id": state.challenge.id,
        "title": state.challenge.title,
        "category": state.challenge.category,
        "state": state.state.value,
        "flag_count": len(flags),
        "solved": state.state.value == "solved" or any(candidate.get("verified") for candidate in flags),
        "solved_flags": [candidate["value"] for candidate in flags if candidate.get("verified")],
        "updated_at": state.updated_at,
        "failure_count": failure_count,
        "latest_observation": latest_observation,
        "hypothesis": hypothesis,
        "metadata": state.metadata,
    }


def _latest_observation(state: ChallengeRunState, events: list[TraceEvent]) -> str:
    for attempt in reversed(state.attempts):
        for step in reversed(attempt.steps):
            if step.observations:
                return step.observations[-1].summary
    for event in reversed(events):
        text = event.stdout or event.stderr
        if text:
            return summarize_text(text, 600) or ""
    return ""


def _current_hypothesis(events: list[TraceEvent], state: ChallengeRunState) -> str:
    for event in reversed(events):
        if event.action in {"specialist-triage", "decision", "plan"}:
            if event.stdout:
                return summarize_text(event.stdout, 600) or ""
            pipeline = event.metadata.get("pipeline") if isinstance(event.metadata, dict) else None
            if isinstance(pipeline, dict) and pipeline.get("hypothesis"):
                return str(pipeline["hypothesis"])
    classification = state.metadata.get("classification")
    if isinstance(classification, dict) and classification.get("category"):
        return f"classified as {classification['category']}"
    return ""


def _challenge_root(context: UIContext, query: dict[str, list[str]]) -> Path:
    raw = query.get("path", [str(context.challenge_root)])[0]
    root = Path(raw).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.resolve()


def _run_dir(context: UIContext, run_id: str) -> Path:
    if "/" in run_id or "\\" in run_id or run_id in {"", ".", ".."}:
        raise ValueError("run id must be a simple directory name")
    path = context.workspace.runs_root / run_id
    resolved = path.resolve()
    root = context.workspace.runs_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("run path is outside workspace")
    if not resolved.is_dir():
        raise FileNotFoundError(f"run not found: {run_id}")
    return resolved


def _safe_child(root: Path, rel: str) -> Path:
    root = root.resolve()
    path = (root / rel).resolve()
    if path != root and root not in path.parents:
        raise ValueError("path is outside run directory")
    return path


def _copy_export(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        if target.exists():
            target = _unique_path(target)
        shutil.copytree(source, target)
    else:
        if target.exists():
            target = _unique_path(target)
        shutil.copy2(source, target)
    return target


def _unique_path(path: Path) -> Path:
    stem, suffix = path.stem, path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not choose export path for {path}")


def windows_path_hint(path: Path) -> str:
    try:
        completed = subprocess.run(["wslpath", "-w", str(path.expanduser())], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
    except OSError:
        pass
    return str(path.expanduser())


def _query_one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _challenge_search_text(row: dict[str, Any]) -> str:
    return " ".join(
        [
            str(row.get("id", "")),
            str(row.get("title", "")),
            str(row.get("category", "")),
            str(row.get("description", "")),
            " ".join(str(item) for item in row.get("files", [])),
        ]
    ).lower()


def _solve_result_dict(result) -> dict[str, Any]:
    return {
        "challenge_id": result.challenge_id,
        "state": result.state.value,
        "solved": result.solved,
        "flags": result.flags,
        "run_dir": str(result.run_dir) if result.run_dir else None,
        "steps_executed": result.steps_executed,
        "metadata": result.metadata,
    }


WORKBENCH_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CTF Agent Workbench</title>
  <link rel="stylesheet" href="/assets/app.css">
</head>
<body>
  <main id="app" class="shell">
    <header class="topbar">
      <div>
        <h1>CTF Agent Workbench</h1>
        <p id="health">连接中</p>
      </div>
      <div class="toolbar">
        <input id="challengePath" value="examples" aria-label="challenge path">
        <button id="refreshBtn" type="button">刷新</button>
      </div>
    </header>
    <section class="grid">
      <aside class="pane nav-pane">
        <div class="filters">
          <input id="searchBox" placeholder="Search">
          <select id="categoryFilter" aria-label="category filter"><option value="all">All categories</option></select>
          <select id="statusFilter" aria-label="status filter">
            <option value="all">All states</option><option value="new">new</option><option value="analyzing">analyzing</option><option value="running">running</option><option value="verifying">verifying</option><option value="solved">solved</option><option value="failed">failed</option><option value="paused">paused</option>
          </select>
          <label class="check"><input id="solvedOnly" type="checkbox"> solved</label>
        </div>
        <div class="pane-head"><h2>Challenges</h2><span id="challengeCount" class="badge">0</span></div>
        <div id="challengeList" class="list"></div>
        <div class="pane-head second"><h2>Runs</h2><span id="runCount" class="badge">0</span></div>
        <div id="runList" class="list"></div>
      </aside>
      <section class="pane run-pane">
        <div class="pane-head"><h2>Current Run</h2><span id="selectedRun" class="badge">none</span></div>
        <div id="runStatus" class="status-grid"></div>
        <div class="actionbar">
          <button id="solveBtn" type="button">Solve</button>
          <button id="resumeBtn" type="button">Resume</button>
          <button id="reportBtn" type="button">Report</button>
          <button id="exportAllBtn" type="button">Export</button>
        </div>
        <div class="observation-band">
          <section><h2>Latest Observation</h2><pre id="latestObservation"></pre></section>
          <section><h2>Current Hypothesis</h2><pre id="currentHypothesis"></pre></section>
        </div>
        <div class="tabs">
          <button class="tab active" data-tab="trace" type="button">Trace</button>
          <button class="tab" data-tab="writeup" type="button">Writeup</button>
        </div>
        <div id="tracePanel" class="tab-panel"></div>
        <div id="writeupPanel" class="tab-panel hidden"><pre id="writeupPreview" class="preview"></pre></div>
      </section>
      <aside class="pane side-pane">
        <div class="tabs side-tabs">
          <button class="side-tab active" data-side="files" type="button">Files</button>
          <button class="side-tab" data-side="artifacts" type="button">Artifacts</button>
          <button class="side-tab" data-side="flags" type="button">Flags</button>
          <button class="side-tab" data-side="notes" type="button">Notes</button>
        </div>
        <div id="filesSide" class="side-panel">
          <div id="fileList" class="file-list"></div>
          <pre id="filePreview" class="preview"></pre>
        </div>
        <div id="artifactsSide" class="side-panel hidden">
          <div id="artifactList" class="file-list"></div>
          <pre id="exportResult" class="mini-output"></pre>
        </div>
        <div id="flagsSide" class="side-panel hidden">
          <div class="pane-head inline"><h2>Flag Candidates</h2><span id="flagCount" class="badge">0</span></div>
          <div id="flagList" class="flag-list"></div>
          <div class="submit-box">
            <input id="manualFlag" placeholder="flag{...}">
            <label class="check"><input id="verifiedFlag" type="checkbox"> verified</label>
            <button id="addFlagBtn" type="button">Add Candidate</button>
            <label class="check"><input id="realSubmit" type="checkbox"> submit</label>
            <input id="confirmText" placeholder="SUBMIT">
            <button id="submitBtn" type="button">Submit</button>
            <pre id="submitResult" class="mini-output"></pre>
          </div>
        </div>
        <div id="notesSide" class="side-panel hidden">
          <textarea id="manualObservation" placeholder="manual observation"></textarea>
          <button id="addObservationBtn" type="button">Add Observation</button>
          <textarea id="manualNotes" placeholder="manual notes"></textarea>
          <button id="saveNotesBtn" type="button">Save Notes</button>
          <pre id="notesResult" class="mini-output"></pre>
        </div>
      </aside>
    </section>
  </main>
  <script src="/assets/app.js"></script>
</body>
</html>
"""


APP_CSS = r"""
:root {
  color-scheme: dark;
  --bg: #151713;
  --panel: #20231d;
  --panel-2: #171a16;
  --line: #3b4036;
  --text: #e7eadf;
  --muted: #a9b09f;
  --accent: #e0b44b;
  --accent-2: #67b38f;
  --bad: #d66f58;
  --warn: #d7a34a;
  --mono: "IBM Plex Mono", "JetBrains Mono", "Cascadia Mono", "SFMono-Regular", monospace;
}
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; background: var(--bg); color: var(--text); font: 13px/1.45 var(--mono); }
button, input, select, textarea { font: inherit; }
.shell { min-height: 100vh; display: grid; grid-template-rows: auto 1fr; }
.topbar { display: flex; justify-content: space-between; gap: 16px; padding: 12px 14px; border-bottom: 1px solid var(--line); background: #11130f; }
h1, h2, p { margin: 0; }
h1 { font-size: 17px; letter-spacing: 0; }
h2 { font-size: 12px; text-transform: uppercase; color: var(--muted); letter-spacing: 0; }
.topbar p { color: var(--muted); margin-top: 3px; overflow-wrap: anywhere; }
.toolbar, .actionbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
input, select, textarea {
  min-width: 0; color: var(--text); background: var(--panel-2); border: 1px solid var(--line);
  padding: 7px 9px; border-radius: 4px;
}
textarea { width: 100%; min-height: 110px; resize: vertical; }
button { color: #12140f; background: var(--accent); border: 1px solid #f0c866; padding: 7px 10px; border-radius: 3px; cursor: pointer; }
button:hover { filter: brightness(1.08); }
.grid { display: grid; grid-template-columns: minmax(280px, 0.95fr) minmax(480px, 1.55fr) minmax(330px, 1fr); min-height: 0; }
.pane { min-width: 0; min-height: 0; border-right: 1px solid var(--line); background: var(--panel); overflow: auto; }
.side-pane { border-right: 0; }
.filters { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 10px; border-bottom: 1px solid var(--line); background: var(--panel-2); }
.filters input { grid-column: span 2; }
.pane-head { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 9px 11px; border-bottom: 1px solid var(--line); background: #1a1d18; }
.pane-head.second { margin-top: 8px; border-top: 1px solid var(--line); }
.pane-head.inline { position: static; }
.badge { color: var(--accent); border: 1px solid var(--line); padding: 2px 6px; border-radius: 2px; }
.list, .flag-list, .file-list { display: grid; align-content: start; }
.item, .flag, .file { padding: 9px 11px; border-bottom: 1px solid var(--line); cursor: pointer; }
.item:hover, .file:hover { background: #282c24; }
.item.active { background: #303424; box-shadow: inset 3px 0 0 var(--accent); }
.meta { color: var(--muted); font-size: 12px; margin-top: 3px; overflow-wrap: anywhere; }
.state-solved { color: var(--accent-2); }
.state-failed { color: var(--bad); }
.state-paused { color: var(--warn); }
.status-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); border-bottom: 1px solid var(--line); }
.kv { padding: 10px 11px; border-right: 1px solid var(--line); }
.kv span { display: block; color: var(--muted); font-size: 11px; }
.kv strong { display: block; margin-top: 4px; overflow-wrap: anywhere; }
.actionbar { padding: 10px 11px; border-bottom: 1px solid var(--line); background: var(--panel-2); }
.observation-band { display: grid; grid-template-columns: 1fr 1fr; border-bottom: 1px solid var(--line); }
.observation-band section { min-width: 0; padding: 10px 11px; border-right: 1px solid var(--line); }
.observation-band pre { min-height: 68px; max-height: 140px; overflow: auto; margin: 8px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; color: #dfe8d7; }
.tabs { display: flex; border-bottom: 1px solid var(--line); background: var(--panel-2); }
.side-tabs { position: sticky; top: 0; z-index: 3; }
.tab, .side-tab { color: var(--muted); background: transparent; border: 0; border-right: 1px solid var(--line); border-radius: 0; }
.tab.active, .side-tab.active { color: var(--text); background: #24281f; box-shadow: inset 0 -2px 0 var(--accent); }
.tab-panel { min-height: 360px; }
.event { display: grid; grid-template-columns: 132px 128px 1fr 56px; gap: 8px; padding: 8px 11px; border-bottom: 1px solid var(--line); }
.event code, .preview, .mini-output { white-space: pre-wrap; overflow-wrap: anywhere; }
.side-panel { min-height: 0; }
.file-list { border-bottom: 1px solid var(--line); max-height: 300px; overflow: auto; }
.preview { margin: 0; padding: 11px; min-height: 220px; color: #dfe8d7; background: #11130f; overflow: auto; }
.hidden { display: none; }
.flag { cursor: default; }
.flag button, .file button { margin-top: 8px; padding: 5px 8px; }
.submit-box { display: grid; gap: 8px; padding: 11px; border-top: 1px solid var(--line); }
.check { color: var(--muted); display: flex; gap: 6px; align-items: center; }
.check input { min-width: auto; }
.mini-output { margin: 0; min-height: 72px; padding: 8px; background: #11130f; border: 1px solid var(--line); }
@media (max-width: 1080px) {
  .grid { grid-template-columns: 1fr; }
  .pane { border-right: 0; border-bottom: 1px solid var(--line); max-height: none; }
  .status-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .event, .observation-band { grid-template-columns: 1fr; }
}
"""


APP_JS = r"""
const state = { selectedRun: null, selectedChallenge: null, runs: [], challenges: [], currentTab: 'trace', sideTab: 'files' };
const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}

async function refreshAll() {
  const health = await api('/api/health');
  $('health').textContent = `${health.workspace} | ${health.windows_artifacts_root}`;
  await loadRuns();
  await loadChallenges();
}

function challengeQuery() {
  const params = new URLSearchParams();
  params.set('path', $('challengePath').value || 'examples');
  params.set('search', $('searchBox').value || '');
  params.set('category', $('categoryFilter').value || 'all');
  params.set('status', $('statusFilter').value || 'all');
  if ($('solvedOnly').checked) params.set('solved', 'true');
  return params.toString();
}

async function loadChallenges() {
  const data = await api(`/api/challenges?${challengeQuery()}`);
  state.challenges = data.challenges;
  $('challengeCount').textContent = data.challenges.length;
  renderCategoryOptions(data.challenges);
  $('challengeList').innerHTML = data.challenges.map((c) => `
    <div class="item ${c.id === state.selectedChallenge ? 'active' : ''}" data-challenge="${escapeHtml(c.id)}" data-run="${escapeHtml(c.run_id)}">
      <strong>${escapeHtml(c.title)}</strong>
      <div class="meta">${escapeHtml(c.id)} · ${escapeHtml(c.category)} · <span class="state-${escapeHtml(c.run_state)}">${escapeHtml(c.run_state)}</span> · solved=${c.solved}</div>
      <div class="meta">${escapeHtml((c.files || []).join(', ') || 'no files')}</div>
    </div>
  `).join('') || emptyItem('no challenges');
  [...$('challengeList').querySelectorAll('[data-challenge]')].forEach((node) => {
    node.addEventListener('click', () => { state.selectedChallenge = node.dataset.challenge; selectRun(node.dataset.run, false); });
  });
}

function renderCategoryOptions(challenges) {
  const current = $('categoryFilter').value || 'all';
  const cats = [...new Set(['all', ...challenges.map((c) => c.category).filter(Boolean)])];
  $('categoryFilter').innerHTML = cats.map((cat) => `<option value="${escapeHtml(cat)}">${escapeHtml(cat)}</option>`).join('');
  $('categoryFilter').value = cats.includes(current) ? current : 'all';
}

async function loadRuns() {
  const data = await api('/api/runs');
  state.runs = data.runs;
  $('runCount').textContent = data.runs.length;
  $('runList').innerHTML = data.runs.map((run) => `
    <div class="item ${run.id === state.selectedRun ? 'active' : ''}" data-run="${escapeHtml(run.id)}">
      <strong>${escapeHtml(run.title)}</strong>
      <div class="meta">${escapeHtml(run.id)} · ${escapeHtml(run.category)} · <span class="state-${escapeHtml(run.state)}">${escapeHtml(run.state)}</span> · failures ${run.failure_count}</div>
      <div class="meta">flags ${run.flag_count} · ${escapeHtml(run.updated_at)}</div>
    </div>
  `).join('') || emptyItem('no runs');
  [...$('runList').querySelectorAll('[data-run]')].forEach((node) => node.addEventListener('click', () => selectRun(node.dataset.run)));
  if (!state.selectedRun && data.runs.length) await selectRun(data.runs[0].id);
  if (!data.runs.length) clearRunPanels();
}

async function selectRun(runId, requireExisting = true) {
  state.selectedRun = runId;
  $('selectedRun').textContent = runId || 'none';
  await loadRuns();
  if (!runId || (!requireExisting && !state.runs.some((r) => r.id === runId))) {
    clearRunPanels();
    return;
  }
  await Promise.all([loadRunState(), loadTrace(), loadFiles(), loadArtifacts(), loadWriteup(false), loadNotes()]);
}

async function loadRunState() {
  if (!state.selectedRun) return;
  const data = await api(`/api/runs/${encodeURIComponent(state.selectedRun)}`);
  const run = data.run;
  const candidates = data.state.flag_candidates || [];
  $('runStatus').innerHTML = [
    ['state', run.state], ['category', run.category], ['failures', run.failure_count], ['flags', run.flag_count], ['run', run.path],
  ].map(([k,v]) => `<div class="kv"><span>${escapeHtml(k)}</span><strong>${escapeHtml(v)}</strong></div>`).join('');
  $('latestObservation').textContent = run.latest_observation || '';
  $('currentHypothesis').textContent = run.hypothesis || '';
  $('flagCount').textContent = candidates.length;
  $('flagList').innerHTML = candidates.map((f) => `
    <div class="flag">
      <strong>${escapeHtml(f.value)}</strong>
      <div class="meta">confidence ${escapeHtml(f.confidence)} · verified ${escapeHtml(f.verified)} · submitted ${escapeHtml(f.submitted)}</div>
      <div class="meta">${escapeHtml(f.source)}</div>
      <button type="button" data-flag="${escapeHtml(f.value)}">Use</button>
    </div>
  `).join('') || emptyItem('no candidates');
  [...$('flagList').querySelectorAll('[data-flag]')].forEach((node) => node.addEventListener('click', () => { $('manualFlag').value = node.dataset.flag; }));
}

async function loadTrace() {
  if (!state.selectedRun) return;
  const data = await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/trace`);
  $('tracePanel').innerHTML = data.events.map((e) => `
    <div class="event">
      <span>${escapeHtml((e.timestamp || '').replace('T', ' ').replace('Z', ''))}</span>
      <span>${escapeHtml(e.agent)} / ${escapeHtml(e.action)}</span>
      <code>${escapeHtml((e.command || []).join(' '))}</code>
      <span>${escapeHtml(e.exit_code ?? '')}</span>
    </div>
  `).join('') || emptyItem('no trace events');
}

async function loadFiles() {
  if (!state.selectedRun) return;
  const data = await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/files?area=work`);
  $('fileList').innerHTML = data.files.map(fileNode).join('') || emptyItem('no files');
  bindFileClicks($('fileList'));
}

async function loadArtifacts() {
  if (!state.selectedRun) return;
  const data = await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/files?area=artifacts`);
  $('artifactList').innerHTML = data.files.map((file) => fileNode(file, true)).join('') || emptyItem('no artifacts');
  bindFileClicks($('artifactList'));
  [...$('artifactList').querySelectorAll('[data-export]')].forEach((node) => node.addEventListener('click', (event) => { event.stopPropagation(); exportPath(node.dataset.export); }));
}

function fileNode(file, exportButton = false) {
  return `<div class="file" data-path="${escapeHtml(file.path)}">
    <strong>${escapeHtml(file.path)}</strong>
    <div class="meta">${file.size} bytes · ${escapeHtml(file.content_type)}</div>
    ${exportButton ? `<button type="button" data-export="${escapeHtml(file.path)}">Export</button>` : ''}
  </div>`;
}

function bindFileClicks(root) {
  [...root.querySelectorAll('[data-path]')].forEach((node) => node.addEventListener('click', () => loadFile(node.dataset.path)));
}

async function loadFile(path) {
  const data = await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/file?path=${encodeURIComponent(path)}`);
  $('filePreview').textContent = `${data.path}\n${data.size} bytes${data.truncated ? ' · truncated' : ''}\n\n${data.text}`;
}

async function loadWriteup(generate) {
  if (!state.selectedRun) return;
  const data = await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/writeup?generate=${generate ? 'true' : 'false'}`);
  $('writeupPreview').textContent = data.text || 'writeup.md not generated yet';
}

async function loadNotes() {
  if (!state.selectedRun) return;
  const data = await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/notes`);
  $('manualNotes').value = data.text || '';
}

async function solveSelected() {
  if (!state.selectedChallenge) return;
  const data = await api(`/api/challenges/${encodeURIComponent(state.selectedChallenge)}/solve`, { method: 'POST', body: JSON.stringify({ executor: 'local', mode: 'single', brain: 'graph', max_steps: 30 }) });
  state.selectedRun = data.run_dir ? data.run_dir.split('/').pop() : state.selectedRun;
  await refreshAll();
}

async function resumeSelected() {
  if (!state.selectedRun) return;
  const data = await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/resume`, { method: 'POST', body: JSON.stringify({ executor: 'local', mode: 'single', brain: 'graph', max_steps: 30 }) });
  $('submitResult').textContent = JSON.stringify(data, null, 2);
  await selectRun(state.selectedRun);
}

async function reportSelected() {
  if (!state.selectedRun) return;
  const data = await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/report`, { method: 'POST', body: '{}' });
  $('writeupPreview').textContent = data.text || '';
  switchTab('writeup');
}

async function exportPath(path = '') {
  if (!state.selectedRun) return;
  const data = await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/export`, { method: 'POST', body: JSON.stringify({ path }) });
  $('exportResult').textContent = JSON.stringify(data, null, 2);
}

async function submitFlag() {
  if (!state.selectedRun) return;
  const payload = { flag: $('manualFlag').value, submit: $('realSubmit').checked, confirm: $('confirmText').value };
  try {
    const data = await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/submit`, { method: 'POST', body: JSON.stringify(payload) });
    $('submitResult').textContent = JSON.stringify(data, null, 2);
    await loadRunState();
  } catch (err) { $('submitResult').textContent = String(err.message || err); }
}

async function addFlag() {
  if (!state.selectedRun) return;
  await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/flag`, { method: 'POST', body: JSON.stringify({ flag: $('manualFlag').value, verified: $('verifiedFlag').checked }) });
  await loadRunState();
}

async function addObservation() {
  if (!state.selectedRun) return;
  const text = $('manualObservation').value;
  const data = await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/observation`, { method: 'POST', body: JSON.stringify({ text }) });
  $('notesResult').textContent = JSON.stringify(data.observation, null, 2);
  $('manualObservation').value = '';
  await selectRun(state.selectedRun);
}

async function saveNotes() {
  if (!state.selectedRun) return;
  const data = await api(`/api/runs/${encodeURIComponent(state.selectedRun)}/notes`, { method: 'POST', body: JSON.stringify({ text: $('manualNotes').value }) });
  $('notesResult').textContent = JSON.stringify(data, null, 2);
  await loadArtifacts();
}

function switchTab(name) {
  state.currentTab = name;
  [...document.querySelectorAll('.tab')].forEach((node) => node.classList.toggle('active', node.dataset.tab === name));
  ['trace', 'writeup'].forEach((tab) => $(tab + 'Panel').classList.toggle('hidden', tab !== name));
  if (name === 'writeup') loadWriteup(true);
}

function switchSide(name) {
  state.sideTab = name;
  [...document.querySelectorAll('.side-tab')].forEach((node) => node.classList.toggle('active', node.dataset.side === name));
  ['files', 'artifacts', 'flags', 'notes'].forEach((tab) => $(tab + 'Side').classList.toggle('hidden', tab !== name));
}

function clearRunPanels() {
  $('runStatus').innerHTML = '';
  $('latestObservation').textContent = '';
  $('currentHypothesis').textContent = '';
  $('tracePanel').innerHTML = emptyItem('no run selected');
  $('fileList').innerHTML = emptyItem('no files');
  $('artifactList').innerHTML = emptyItem('no artifacts');
  $('flagList').innerHTML = emptyItem('no candidates');
  $('flagCount').textContent = '0';
  $('writeupPreview').textContent = '';
}

function emptyItem(text) { return `<div class="item"><span class="meta">${escapeHtml(text)}</span></div>`; }

document.addEventListener('click', (event) => {
  if (event.target.matches('.tab')) switchTab(event.target.dataset.tab);
  if (event.target.matches('.side-tab')) switchSide(event.target.dataset.side);
});
['searchBox', 'categoryFilter', 'statusFilter', 'solvedOnly'].forEach((id) => $(id).addEventListener('input', loadChallenges));
$('refreshBtn').addEventListener('click', refreshAll);
$('solveBtn').addEventListener('click', solveSelected);
$('resumeBtn').addEventListener('click', resumeSelected);
$('reportBtn').addEventListener('click', reportSelected);
$('exportAllBtn').addEventListener('click', () => exportPath(''));
$('submitBtn').addEventListener('click', submitFlag);
$('addFlagBtn').addEventListener('click', addFlag);
$('addObservationBtn').addEventListener('click', addObservation);
$('saveNotesBtn').addEventListener('click', saveNotes);
refreshAll().catch((err) => { $('health').textContent = String(err.message || err); clearRunPanels(); });
"""
