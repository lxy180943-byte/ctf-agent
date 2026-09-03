import json
from pathlib import Path

from ctf_agent.cli.app import main
from ctf_agent.core.models import FlagCandidate
from ctf_agent.core.state import ChallengeRunState
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.platforms.base import SubmissionResult


class FakeAdapter:
    class Profile:
        name = "demo"

    def __init__(self):
        self.profile = self.Profile()
        self.submissions = []

    def list_challenges(self):
        from ctf_agent.core.models import Challenge

        return [Challenge(id="7", title="Warmup", category="misc", metadata={"dynamic_scoring": {"value": 100, "solved_by_me": False}, "locked": False, "hidden": False})]

    def get_challenge(self, challenge_id):
        from ctf_agent.core.models import Challenge

        return Challenge(id=str(challenge_id), title="Warmup", category="misc", files=["flag.txt"], metadata={"source": "ctfd", "profile": "demo"})

    def download_files(self, challenge, destination):
        from ctf_agent.core.models import Artifact

        path = Path(destination) / "flag.txt"
        path.write_text("flag{ctfd_cli}\n", encoding="utf-8")
        return [Artifact(path=str(path), kind="challenge-file", metadata={"source": "mock", "sha256": "abc", "size": 15})]

    def submit_flag(self, challenge, flag, *, submit=False, confirm=None):
        self.submissions.append((challenge.id, flag, submit, confirm))
        return SubmissionResult(challenge_id=challenge.id, flag=flag, submitted=submit and confirm == "SUBMIT demo 7", accepted=True if submit and confirm == "SUBMIT demo 7" else None, message="ok")

    def confirmation_string(self, challenge):
        return f"SUBMIT demo {challenge.id}"


def test_ctfd_list_cli_uses_profile(monkeypatch, capsys):
    fake = FakeAdapter()
    monkeypatch.setattr("ctf_agent.cli.app.adapter_from_config", lambda config, profile: fake)
    assert main(["ctfd", "list", "--profile", "demo"]) == 0
    assert "Warmup" in capsys.readouterr().out


def test_ctfd_pull_cli_downloads_to_workspace(monkeypatch, capsys, tmp_path):
    fake = FakeAdapter()
    monkeypatch.setattr("ctf_agent.cli.app.adapter_from_config", lambda config, profile: fake)
    monkeypatch.setenv("CTF_AGENT_WORKSPACE_DIR", str(tmp_path / "workspace"))
    assert main(["ctfd", "pull", "7", "--profile", "demo"]) == 0
    out = capsys.readouterr().out
    assert "run_dir:" in out
    assert (tmp_path / "workspace" / "runs" / "7" / "work" / "flag.txt").exists()


def test_ctfd_submit_cli_requires_confirm(monkeypatch, capsys, tmp_path):
    fake = FakeAdapter()
    monkeypatch.setattr("ctf_agent.cli.app.adapter_from_config", lambda config, profile: fake)
    manager = WorkspaceManager(tmp_path / "workspace")
    state = manager.init_state(fake.get_challenge("7"))
    state.add_flag_candidate(FlagCandidate(value="flag{ctfd_cli}", source="test", confidence=1.0, verified=True))
    manager.save_state(state)
    run_dir = manager.layout_for("7").challenge_dir

    assert main(["ctfd", "submit", str(run_dir), "--profile", "demo", "--submit"]) == 1
    assert "submitted: False" in capsys.readouterr().out

    assert main(["ctfd", "submit", str(run_dir), "--profile", "demo", "--submit", "--confirm", "SUBMIT demo 7", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["submitted"] is True
    assert fake.submissions[-1] == ("7", "flag{ctfd_cli}", True, "SUBMIT demo 7")
