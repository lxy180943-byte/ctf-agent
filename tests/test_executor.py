import json
from pathlib import Path

import pytest

from ctf_agent.core.trace import TraceStore
from ctf_agent.sandbox import DockerExecutor, LocalExecutor, docker_available, image_for_category
from ctf_agent.sandbox.executor import CommandSafetyError, WorkspaceBoundaryError
from ctf_agent.sandbox.images import BUILDABLE_PROFILES, DOCKER_PROFILES, build_profile, docker_profiles_doctor, dockerfile_path, image_exists


def test_local_executor_runs_inside_workspace_and_writes_trace(tmp_path):
    workspace = tmp_path / "workspace"
    cwd = workspace / "runs" / "demo" / "work"
    trace = TraceStore(workspace / "runs" / "demo" / "trace.jsonl")
    executor = LocalExecutor(workspace, trace_store=trace, challenge_id="demo")

    result = executor.run("printf hello", cwd=cwd, timeout=5, env={"DEMO": "1"})

    assert result.ok is True
    assert result.stdout == "hello"
    events = trace.read_events()
    assert len(events) == 1
    assert events[0].metadata["cwd"] == str(cwd.resolve())
    assert events[0].metadata["env"] == {"DEMO": "1"}
    assert events[0].metadata["timeout"] == 5
    assert len(events[0].artifacts) == 2
    assert Path(events[0].artifacts[0].path).exists()


def test_local_executor_rejects_cwd_outside_workspace(tmp_path):
    executor = LocalExecutor(tmp_path / "workspace")
    with pytest.raises(WorkspaceBoundaryError):
        executor.run("true", cwd=tmp_path / "outside")


def test_local_executor_rejects_destructive_path_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    cwd = workspace / "runs" / "demo" / "work"
    executor = LocalExecutor(workspace)
    with pytest.raises(CommandSafetyError):
        executor.run("rm -rf /tmp/not-owned-by-workspace", cwd=cwd)


def test_local_executor_allows_destructive_path_inside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    cwd = workspace / "runs" / "demo" / "work"
    target = cwd / "scratch.txt"
    target.parent.mkdir(parents=True)
    target.write_text("delete me", encoding="utf-8")
    executor = LocalExecutor(workspace)
    result = executor.run("rm ./scratch.txt", cwd=cwd)
    assert result.exit_code == 0
    assert not target.exists()


def test_local_executor_timeout_records_exit_124(tmp_path):
    workspace = tmp_path / "workspace"
    cwd = workspace / "runs" / "demo" / "work"
    executor = LocalExecutor(workspace)
    result = executor.run("sleep 2", cwd=cwd, timeout=1)
    assert result.exit_code == 124
    assert result.timed_out is True


def test_trace_summarizes_but_artifact_keeps_full_stdout(tmp_path):
    workspace = tmp_path / "workspace"
    cwd = workspace / "runs" / "demo" / "work"
    trace = TraceStore(workspace / "runs" / "demo" / "trace.jsonl")
    executor = LocalExecutor(workspace, trace_store=trace, challenge_id="demo")
    result = executor.run("python3 -c 'print(\"A\" * 5000)'", cwd=cwd)
    event_line = trace.path.read_text(encoding="utf-8").splitlines()[0]
    data = json.loads(event_line)
    assert "<truncated" in data["stdout"]
    stdout_artifact = [artifact for artifact in result.artifacts if artifact.kind == "stdout"][0]
    assert "A" * 5000 in Path(stdout_artifact.path).read_text(encoding="utf-8")


def test_docker_executor_command_mounts_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    executor = DockerExecutor(workspace, image="ctf-agent:generic", network="none", memory="128m", cpu=0.5)
    command = executor._docker_command("pwd", Path("/workspace/runs/demo/work"), timeout=3, env={"X": "Y"})
    assert command[:2] == ["docker", "run"]
    assert "--network" in command
    assert "none" in command
    assert f"{workspace.resolve()}:/workspace" in command
    assert "-e" in command
    assert "X=Y" in command


def test_image_for_category_uses_profile_fallback():
    config = {"sandbox": {"images": {"generic": "generic-image", "pwn": "pwn-image"}}}
    assert image_for_category(config, "pwn") == "pwn-image"
    assert image_for_category(config, "rev") == "generic-image"


def test_docker_profiles_have_dockerfiles_and_core_checks():
    assert set(BUILDABLE_PROFILES) == {"generic", "pwn", "web", "crypto", "rev", "forensics"}
    for name in BUILDABLE_PROFILES:
        profile = DOCKER_PROFILES[name]
        assert profile.image == f"ctf-agent:{name}"
        assert profile.dockerfile == f"docker/Dockerfile.{name}"
        assert profile.core_checks
        assert dockerfile_path(profile).exists()


def test_build_profile_constructs_expected_docker_build(monkeypatch):
    calls = []

    class Completed:
        returncode = 0
        stdout = "ok"

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Completed()

    monkeypatch.setattr("ctf_agent.sandbox.images.subprocess.run", fake_run)
    result = build_profile("generic", no_cache=True, pull=True)
    assert result["ok"] is True
    assert calls[0][:3] == ["docker", "build", "-f"]
    assert "-t" in calls[0]
    assert "ctf-agent:generic" in calls[0]
    assert "--pull" in calls[0]
    assert "--no-cache" in calls[0]


def test_docker_profiles_doctor_reports_unavailable(monkeypatch):
    monkeypatch.setattr("ctf_agent.sandbox.images.docker_available", lambda: False)
    report = docker_profiles_doctor(run_tools=True)
    assert report["ok"] is False
    assert report["docker_available"] is False
    assert len(report["profiles"]) == len(BUILDABLE_PROFILES)


@pytest.mark.integration
def test_docker_executor_smoke_if_available(tmp_path):
    if not docker_available():
        pytest.skip("Docker is not available")
    image = DOCKER_PROFILES["generic"].image
    if not image_exists(image):
        pytest.skip(f"Docker image not present locally: {image}; run `make docker-build-generic`")

    workspace = tmp_path / "workspace"
    cwd = workspace / "runs" / "demo" / "work"
    executor = DockerExecutor(workspace, image=image, network="none")
    result = executor.run("python3 -c 'print(42)'", cwd=cwd, timeout=10)
    assert result.exit_code == 0
    assert result.stdout.strip() == "42"


@pytest.mark.integration
def test_docker_profile_core_tools_if_images_available():
    if not docker_available():
        pytest.skip("Docker is not available")
    missing = [DOCKER_PROFILES[name].image for name in BUILDABLE_PROFILES if not image_exists(DOCKER_PROFILES[name].image)]
    if missing:
        pytest.skip("Docker profile images are not built locally: " + ", ".join(missing))
    report = docker_profiles_doctor(run_tools=True)
    assert report["ok"] is True
    assert all(profile["checks"] for profile in report["profiles"])
