import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ctf_agent.core.models import Challenge, FlagCandidate
from ctf_agent.core.reporter import Reporter
from ctf_agent.core.state import ChallengeState
from ctf_agent.core.trace import TraceEvent
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.ui.server import ThreadedWorkbenchServer, make_server


def request_json(url: str, *, data: dict | None = None):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def request_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read().decode("utf-8")


@pytest.fixture
def ui_server(tmp_path):
    challenge_root = tmp_path / "challenges"
    challenge_dir = challenge_root / "toy"
    challenge_dir.mkdir(parents=True)
    (challenge_dir / "challenge.yaml").write_text(
        "id: toy\ntitle: Toy UI\ncategory: misc\nfiles:\n  - flag.txt\nflag_regex: flag\\{[A-Za-z0-9_]+\\}\n",
        encoding="utf-8",
    )
    (challenge_dir / "flag.txt").write_text("flag{ui_toy}\n", encoding="utf-8")

    workspace = tmp_path / "workspace"
    manager = WorkspaceManager(workspace)
    state = manager.init_state(Challenge(id="toy", title="Toy UI", category="misc", files=["flag.txt"]))
    layout = manager.layout_for("toy")
    (layout.work_dir / "flag.txt").write_text("flag{ui_toy}\n", encoding="utf-8")
    state.state = ChallengeState.SOLVED
    state.add_flag_candidate(FlagCandidate(value="flag{ui_toy}", source="test", confidence=0.9, verified=True))
    manager.save_state(state)
    manager.trace_store_for("toy").append(
        TraceEvent(
            challenge_id="toy",
            agent="executor",
            action="run-command",
            command=["bash", "-lc", "cat flag.txt"],
            stdout="flag{ui_toy}",
            exit_code=0,
        )
    )
    Reporter(workspace).generate(layout.challenge_dir)
    config = {
        "workspace_dir": str(workspace),
        "sandbox": {"engine": "local", "timeout_seconds": 10},
        "submit": {"dry_run_default": True},
        "ui": {"challenge_root": str(challenge_root)},
    }
    server = ThreadedWorkbenchServer(make_server(config, port=0, challenge_root=challenge_root)).start()
    try:
        yield server
    finally:
        server.stop()


def test_ui_serves_workbench_and_health(ui_server):
    html = request_text(ui_server.url + "/")
    assert "CTF Agent Workbench" in html
    assert "Flag Candidates" in html
    health = request_json(ui_server.url + "/api/health")
    assert health["ok"] is True
    assert "workspace" in health


def test_ui_api_lists_challenges_runs_trace_files_and_writeup(ui_server):
    challenges = request_json(ui_server.url + "/api/challenges")["challenges"]
    assert challenges[0]["id"] == "toy"
    runs = request_json(ui_server.url + "/api/runs")["runs"]
    assert runs[0]["id"] == "toy"
    state = request_json(ui_server.url + "/api/runs/toy")
    assert state["run"]["state"] == "solved"
    assert state["state"]["flag_candidates"][0]["value"] == "flag{ui_toy}"
    trace = request_json(ui_server.url + "/api/runs/toy/trace")["events"]
    assert trace[0]["agent"] == "executor"
    files = request_json(ui_server.url + "/api/runs/toy/files")["files"]
    assert any(item["path"] == "work/flag.txt" for item in files)
    file_data = request_json(ui_server.url + "/api/runs/toy/file?path=work/flag.txt")
    assert "flag{ui_toy}" in file_data["text"]
    writeup = request_json(ui_server.url + "/api/runs/toy/writeup?generate=true")
    assert "Toy UI" in writeup["text"]


def test_ui_submit_defaults_to_dry_run_and_requires_confirmation_for_real_submit(ui_server):
    result = request_json(ui_server.url + "/api/runs/toy/submit", data={"flag": "flag{ui_toy}"})
    assert result["dry_run"] is True
    assert result["submitted"] is False
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        request_json(ui_server.url + "/api/runs/toy/submit", data={"flag": "flag{ui_toy}", "submit": True})
    assert excinfo.value.code == 400



@pytest.fixture
def empty_ui_server(tmp_path):
    challenge_root = tmp_path / "challenges"
    challenge_root.mkdir()
    workspace = tmp_path / "workspace"
    config = {
        "workspace_dir": str(workspace),
        "artifacts_dir": str(tmp_path / "exports"),
        "sandbox": {"engine": "local", "timeout_seconds": 10},
        "submit": {"dry_run_default": True},
        "ui": {"challenge_root": str(challenge_root)},
    }
    server = ThreadedWorkbenchServer(make_server(config, port=0, challenge_root=challenge_root)).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def competition_ui_server(tmp_path):
    challenge_root = tmp_path / "challenges"
    challenge_dir = challenge_root / "web-toy"
    challenge_dir.mkdir(parents=True)
    (challenge_dir / "challenge.yaml").write_text(
        "id: web-toy\n"
        "title: Web Toy\n"
        "category: web\n"
        "description: form flag\n"
        "files:\n"
        "  - index.html\n"
        "flag_regex: flag\\{[A-Za-z0-9_]+\\}\n",
        encoding="utf-8",
    )
    (challenge_dir / "index.html").write_text("<form><input value='flag{ui_solve}'></form>\n", encoding="utf-8")
    config = {
        "workspace_dir": str(tmp_path / "workspace"),
        "artifacts_dir": str(tmp_path / "exports"),
        "sandbox": {"engine": "local", "timeout_seconds": 10},
        "memory": {"enabled": False, "auto_learn": False},
        "submit": {"dry_run_default": True},
        "ui": {"challenge_root": str(challenge_root)},
    }
    server = ThreadedWorkbenchServer(make_server(config, port=0, challenge_root=challenge_root)).start()
    try:
        yield server
    finally:
        server.stop()


def test_ui_empty_workspace_smoke_does_not_crash(empty_ui_server):
    html = request_text(empty_ui_server.url + "/")
    assert "Current Run" in html
    assert "Artifacts" in html
    runs = request_json(empty_ui_server.url + "/api/runs")
    assert runs["runs"] == []
    challenges = request_json(empty_ui_server.url + "/api/challenges")
    assert challenges["challenges"] == []
    health = request_json(empty_ui_server.url + "/api/health")
    assert health["windows_artifacts_root"]


def test_ui_challenge_filters_and_solve_resume_report_export_takeover(competition_ui_server):
    challenges = request_json(competition_ui_server.url + "/api/challenges?category=web&search=toy&solved=false")
    assert [item["id"] for item in challenges["challenges"]] == ["web-toy"]

    solve = request_json(
        competition_ui_server.url + "/api/challenges/web-toy/solve",
        data={"executor": "local", "mode": "specialist", "brain": "fallback", "max_steps": 20},
    )
    assert solve["solved"] is True
    run_id = Path(solve["run_dir"]).name

    state = request_json(competition_ui_server.url + f"/api/runs/{run_id}")
    assert state["run"]["state"] == "solved"
    assert state["run"]["latest_observation"]
    assert "triage" in state["run"]["hypothesis"].lower()

    report = request_json(competition_ui_server.url + f"/api/runs/{run_id}/report", data={})
    assert "Web Toy" in report["text"]

    resumed = request_json(competition_ui_server.url + f"/api/runs/{run_id}/resume", data={"executor": "local", "brain": "fallback", "max_steps": 5})
    assert resumed["solved"] is True

    manual_obs = request_json(competition_ui_server.url + f"/api/runs/{run_id}/observation", data={"text": "manual browser finding"})
    assert manual_obs["observation"]["metadata"]["manual"] is True

    manual_flag = request_json(competition_ui_server.url + f"/api/runs/{run_id}/flag", data={"flag": "flag{manual_ui}", "verified": True})
    assert manual_flag["candidate"]["verified"] is True

    notes = request_json(competition_ui_server.url + f"/api/runs/{run_id}/notes", data={"text": "burp note"})
    assert "manual-notes.md" in notes["path"]

    files = request_json(competition_ui_server.url + f"/api/runs/{run_id}/files?area=artifacts")["files"]
    assert any(item["path"].endswith("manual-notes.md") for item in files)

    exported = request_json(competition_ui_server.url + f"/api/runs/{run_id}/export", data={"path": "artifacts/manual-notes.md"})
    assert exported["exported"]
    assert Path(exported["exported"][0]["target"]).exists()
    assert exported["windows_path"]

    dry = request_json(competition_ui_server.url + f"/api/runs/{run_id}/submit", data={"flag": "flag{manual_ui}"})
    assert dry["dry_run"] is True
    assert dry["submitted"] is False

    real = request_json(competition_ui_server.url + f"/api/runs/{run_id}/submit", data={"flag": "flag{manual_ui}", "submit": True, "confirm": "SUBMIT"})
    assert real["submitted"] is True
