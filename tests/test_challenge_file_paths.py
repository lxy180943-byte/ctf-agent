from __future__ import annotations

from pathlib import Path

import pytest

from ctf_agent.agents import AgentContext
from ctf_agent.core.models import Challenge
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.graph.nodes import collect_initial_evidence
from ctf_agent.graph.state import initial_workflow_state
from ctf_agent.platforms.local import LocalPlatformAdapter
from ctf_agent.pydantic_agent.tools import ReadFileInput, ToolDependencies, read_file, visible_workspace_paths
from ctf_agent.sandbox import LocalExecutor
from ctf_agent.tools import default_registry


def _challenge(tmp_path: Path, files: list[str]) -> Challenge:
    source = tmp_path / "challenge"
    source.mkdir(parents=True, exist_ok=True)
    return Challenge(id="nested", title="Nested", category="web", files=files, metadata={"source_dir": str(source)})


def _deps(tmp_path: Path, challenge: Challenge) -> ToolDependencies:
    manager = WorkspaceManager(tmp_path / "workspace")
    state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    trace = manager.trace_store_for(challenge.id)
    return ToolDependencies(
        context=AgentContext(
            state=state,
            layout=layout,
            trace_store=trace,
            executor=LocalExecutor(manager.workspace_root, trace_store=trace, challenge_id=challenge.id),
            tool_registry=default_registry(),
            config={},
            max_steps=3,
            timeout=5,
        )
    )


def test_nested_challenge_file_download_and_read_uses_logical_relative_path(tmp_path: Path) -> None:
    challenge = _challenge(tmp_path, ["src/app.php"])
    source = Path(challenge.metadata["source_dir"])
    (source / "src").mkdir(parents=True)
    (source / "src" / "app.php").write_text("<?php echo 'nested-marker';", encoding="utf-8")
    deps = _deps(tmp_path, challenge)

    artifacts = LocalPlatformAdapter(source).download_files(challenge, deps.context.layout.work_dir)

    assert (deps.context.layout.work_dir / "src" / "app.php").is_file()
    assert not (deps.context.layout.work_dir / "app.php").exists()
    assert artifacts[0].metadata["logical_path"] == "src/app.php"
    assert visible_workspace_paths(deps) == ["src/app.php"]
    result = read_file(deps, ReadFileInput(path="src/app.php"))
    assert result.ok is True
    assert result.observation["path"] == "src/app.php"
    assert "nested-marker" in result.observation["body_excerpt"]


def test_collect_initial_evidence_reads_run_work_recursive_paths(tmp_path: Path) -> None:
    challenge = _challenge(tmp_path, ["src/app.php"])
    deps = _deps(tmp_path, challenge)
    (deps.context.layout.work_dir / "src").mkdir(parents=True)
    (deps.context.layout.work_dir / "src" / "app.php").write_text("<?php", encoding="utf-8")
    state = initial_workflow_state(challenge, run_dir=deps.context.layout.challenge_dir)

    result = collect_initial_evidence(state)

    files = result["observations"][0]["files"]
    assert files == [{"path": "src/app.php", "size": 5}]


@pytest.mark.parametrize("bad_path", ["../secret.txt", "src/../../secret.txt", "/tmp/secret.txt", "C:/secret.txt", ""])
def test_download_files_rejects_traversal_absolute_and_empty_paths(tmp_path: Path, bad_path: str) -> None:
    challenge = _challenge(tmp_path, [bad_path])
    with pytest.raises(ValueError):
        LocalPlatformAdapter(challenge.metadata["source_dir"]).download_files(challenge, tmp_path / "dest")


def test_download_files_rejects_destination_collision(tmp_path: Path) -> None:
    challenge = _challenge(tmp_path, ["a.txt", "./a.txt"])
    source = Path(challenge.metadata["source_dir"])
    (source / "a.txt").write_text("a", encoding="utf-8")
    with pytest.raises(ValueError):
        LocalPlatformAdapter(source).download_files(challenge, tmp_path / "dest")


def test_read_file_rejects_absolute_and_traversal_paths(tmp_path: Path) -> None:
    challenge = _challenge(tmp_path, ["a.txt"])
    deps = _deps(tmp_path, challenge)
    (deps.context.layout.work_dir / "a.txt").write_text("a", encoding="utf-8")

    for bad_path in ("../a.txt", "/tmp/a.txt", "C:/tmp/a.txt"):
        result = read_file(deps, ReadFileInput(path=bad_path))
        assert result.ok is False
        assert result.risk == "refuse"
