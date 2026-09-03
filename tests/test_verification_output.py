import json
from pathlib import Path

from ctf_agent.agents import AgentContext, ExecutionBatch, VerifierAgent
from ctf_agent.core.flag_detector import FlagDetector
from ctf_agent.core.models import Artifact, Challenge, FlagCandidate
from ctf_agent.core.reporter import Reporter
from ctf_agent.core.state import ChallengeRunState, ChallengeState
from ctf_agent.core.submitter import Submitter
from ctf_agent.core.trace import TraceEvent
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.platforms.base import SubmissionResult
from ctf_agent.sandbox import ExecutionResult, LocalExecutor
from ctf_agent.tools import default_registry


def test_flag_detector_uses_challenge_regex_common_and_custom_patterns():
    detector = FlagDetector(flag_regex=r"CTF\[[A-Z0-9_]+\]", custom_patterns=[r"TOKEN-[0-9]{3}"])
    candidates = detector.detect_text("flag{common} CTF[CUSTOM_FLAG] TOKEN-123 flag{common}", "stdout")
    assert [candidate.value for candidate in candidates] == ["CTF[CUSTOM_FLAG]", "TOKEN-123", "flag{common}"]
    assert candidates[0].confidence > candidates[1].confidence > candidates[2].confidence


def test_flag_detector_detects_from_files_and_artifacts(tmp_path):
    file_path = tmp_path / "note.txt"
    file_path.write_text("flag{from_file}", encoding="utf-8")
    artifact = Artifact(path=str(file_path), kind="stdout")
    detector = FlagDetector()
    assert detector.detect_file(file_path)[0].value == "flag{from_file}"
    assert detector.detect_artifacts([artifact])[0].source.startswith("artifact:")


def test_verifier_extracts_from_workspace_file_even_when_stdout_empty(tmp_path):
    challenge = Challenge(id="file-only", title="File Only", category="misc", files=["secret.txt"])
    manager = WorkspaceManager(tmp_path / "workspace")
    state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    layout.work_dir.mkdir(parents=True, exist_ok=True)
    (layout.work_dir / "secret.txt").write_text("flag{workspace_file}", encoding="utf-8")
    batch = ExecutionBatch(
        results=[
            ExecutionResult(
                command="true",
                cwd=str(layout.work_dir),
                env={},
                timeout=10,
                exit_code=0,
                stdout="",
                stderr="",
                started_at="2026-08-31T00:00:00Z",
                ended_at="2026-08-31T00:00:01Z",
                duration_seconds=1.0,
            )
        ]
    )
    context = AgentContext(
        state=state,
        layout=layout,
        trace_store=manager.trace_store_for(challenge.id),
        executor=LocalExecutor(tmp_path / "workspace"),
        tool_registry=default_registry(),
        config={},
        max_steps=10,
        timeout=30,
        metadata={"execution_batch": batch},
    )
    result = VerifierAgent().run(context)
    assert result.candidates[0].value == "flag{workspace_file}"
    assert result.candidates[0].source == "file:secret.txt"


def test_submitter_local_dry_run_keeps_external_submit_false(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspace")
    state = manager.init_state(Challenge(id="local", title="Local", category="misc"))
    state.state = ChallengeState.SOLVED
    state.add_flag_candidate(FlagCandidate(value="flag{local}", source="test", confidence=0.9, verified=True))
    manager.save_state(state)
    result = Submitter({"workspace_dir": str(tmp_path / "workspace")}).submit_run(manager.layout_for("local").challenge_dir)
    restored = manager.load_state("local")
    assert result.dry_run is True
    assert result.submitted is False
    assert restored.metadata["last_submit"]["dry_run"] is True


def test_submitter_local_submit_marks_candidate_submitted(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspace")
    state = manager.init_state(Challenge(id="local", title="Local", category="misc"))
    state.state = ChallengeState.FAILED
    state.add_flag_candidate(FlagCandidate(value="flag{local}", source="test", confidence=0.9, verified=True))
    manager.save_state(state)
    result = Submitter({"workspace_dir": str(tmp_path / "workspace")}).submit_run(manager.layout_for("local").challenge_dir, submit=True)
    restored = manager.load_state("local")
    assert result.submitted is True
    assert restored.state is ChallengeState.SOLVED
    assert restored.flag_candidates[0].submitted is True


def test_submitter_ctfd_dry_run_does_not_real_submit(tmp_path, monkeypatch):
    manager = WorkspaceManager(tmp_path / "workspace")
    challenge = Challenge(id="7", title="CTFd", category="misc", metadata={"source": "ctfd"})
    state = manager.init_state(challenge)
    state.state = ChallengeState.SOLVED
    state.add_flag_candidate(FlagCandidate(value="flag{ctfd}", source="test", confidence=0.9, verified=True))
    manager.save_state(state)
    calls = []

    class FakeCTFd:
        def submit_flag(self, challenge, flag, *, submit=False, confirm=None):
            calls.append((submit, confirm))
            return SubmissionResult(challenge_id=challenge.id, flag=flag, submitted=submit, accepted=None, message="dry")

    monkeypatch.setattr("ctf_agent.core.submitter.adapter_from_config", lambda config, profile=None: FakeCTFd())
    result = Submitter({"workspace_dir": str(tmp_path / "workspace"), "platform": {"ctfd": {"url": "https://ctf.example", "token": "t"}}}).submit_run(
        manager.layout_for("7").challenge_dir
    )
    assert result.dry_run is True
    assert calls == [(False, None)]


def test_reporter_generates_writeup_with_commands_and_flag(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspace")
    challenge = Challenge(id="report", title="Report Toy", category="misc", description="demo", files=["prompt.txt"], metadata={"source_dir": "examples/report"})
    state = manager.init_state(challenge)
    state.state = ChallengeState.SOLVED
    state.add_flag_candidate(FlagCandidate(value="flag{report}", source="test", confidence=0.9, verified=True))
    manager.save_state(state)
    trace = manager.trace_store_for("report")
    trace.append(
        TraceEvent(
            challenge_id="report",
            agent="executor",
            action="run-command",
            command=["bash", "-lc", "cat prompt.txt"],
            stdout="flag{report}",
            exit_code=0,
            started_at="2026-08-31T00:00:00Z",
            ended_at="2026-08-31T00:00:01Z",
        )
    )
    report_path = Reporter(tmp_path / "workspace").generate(manager.layout_for("report").challenge_dir)
    text = report_path.read_text(encoding="utf-8")
    assert "# Report Toy" in text
    assert "cat prompt.txt" in text
    assert "flag{report}" in text
