from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ctf_agent.agents import AgentContext
from ctf_agent.core.models import Challenge
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.graph.builder import build_workflow
from ctf_agent.graph.edges import after_verify
from ctf_agent.graph.nodes import NodeRuntime, bind_runtime, clear_runtime
from ctf_agent.graph.state import initial_workflow_state
from ctf_agent.pydantic_agent.tools import ToolDependencies
from ctf_agent.sandbox import LocalExecutor
from ctf_agent.tools import default_registry


def _plan(path: str, marker: str) -> dict:
    return {
        "goal": f"Read {path}",
        "action_type": "read_file",
        "action_input": {"type": "read_file", "path": path},
        "expected_signal": marker,
        "failure_signal": "missing",
        "risk": "low",
        "rollback": "none",
    }


def _decision(plan: dict, name: str) -> dict:
    return {
        "current_hypothesis": {
            "name": name,
            "claim": f"Use {plan['action_input']['path']} to collect the next evidence item.",
            "evidence_for": [],
            "evidence_against": [],
            "confidence": 0.5,
            "falsification_test": "The selected file does not contain the expected marker.",
        },
        "confirmed_facts": [],
        "unknowns": ["next evidence"],
        "candidate_chains": [[name]],
        "selected_experiment": plan,
        "next_action": "execute_selected_experiment",
        "need_human": False,
        "stop_reason": None,
    }


def _context(tmp_path: Path) -> tuple[Challenge, AgentContext]:
    challenge = Challenge(id="reason-after-evidence", title="Reason after evidence", category="misc", files=["a.txt", "b.txt"])
    manager = WorkspaceManager(tmp_path / "workspace")
    state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    (layout.work_dir / "a.txt").write_text("alpha evidence", encoding="utf-8")
    (layout.work_dir / "b.txt").write_text("beta evidence", encoding="utf-8")
    trace = manager.trace_store_for(challenge.id)
    context = AgentContext(
        state=state,
        layout=layout,
        trace_store=trace,
        executor=LocalExecutor(manager.workspace_root, trace_store=trace, challenge_id=challenge.id),
        tool_registry=default_registry(),
        config={},
        max_steps=5,
        timeout=5,
    )
    return challenge, context


def _has_read_observation(state: dict, path: str, marker: str) -> bool:
    for observation in state.get("observations", []):
        if not isinstance(observation, dict) or observation.get("source") != "read_file":
            continue
        evidence = observation.get("evidence") if isinstance(observation.get("evidence"), dict) else {}
        if evidence.get("path") == path and marker in str(evidence.get("body_excerpt") or ""):
            return True
    return False


def _has_evidence_delta_for_path(state: dict, path: str) -> bool:
    for delta in state.get("evidence_deltas", []):
        if path in str(delta):
            return True
    return False


def test_compiled_graph_rereasons_after_new_evidence_before_selecting_next_experiment(tmp_path: Path, monkeypatch) -> None:
    challenge, context = _context(tmp_path)
    snapshots: list[dict] = []
    plans = [_plan("a.txt", "alpha evidence"), _plan("b.txt", "beta evidence")]

    def unsolved_verifier(*_args, **_kwargs):
        return SimpleNamespace(observation={"candidate_count": 0, "verified_count": 0, "sources": []})

    monkeypatch.setattr("ctf_agent.graph.nodes.ask_verifier", unsolved_verifier)

    def reasoner(workflow_state: dict):
        snapshots.append(
            {
                "observations": list(workflow_state.get("observations", [])),
                "evidence_deltas": list(workflow_state.get("evidence_deltas", [])),
                "tool_calls": list(workflow_state.get("tool_calls", [])),
                "experiment_assessments": list(workflow_state.get("experiment_assessments", [])),
            }
        )
        if len(snapshots) == 1:
            assert not _has_read_observation(workflow_state, "a.txt", "alpha evidence")
            assert not workflow_state.get("evidence_deltas")
            return _decision(plans[0], "decision-a")
        if len(snapshots) == 2:
            assert _has_read_observation(workflow_state, "a.txt", "alpha evidence")
            assert _has_evidence_delta_for_path(workflow_state, "a.txt")
            assert [call.get("action_input", {}).get("path") for call in workflow_state.get("tool_calls", [])] == ["a.txt"]
            return _decision(plans[1], "decision-b")
        raise AssertionError("workflow should stop by tool budget after executing decision B")

    bind_runtime(context.layout.challenge_dir, NodeRuntime(tools=ToolDependencies(context=context), reasoner=reasoner))
    try:
        workflow = build_workflow(max_tool_calls=2)
        final = workflow.invoke(
            initial_workflow_state(challenge, run_dir=context.layout.challenge_dir, max_iterations=5),
            config={"configurable": {"thread_id": "reason-after-evidence"}, "recursion_limit": 30},
        )
    finally:
        clear_runtime(context.layout.challenge_dir)

    execution_order = [call.get("action_input", {}).get("path") for call in final["tool_calls"]]
    assert len(snapshots) == 2
    assert execution_order == ["a.txt", "b.txt"]
    assert final["phase"] == "failed"
    assert [event["decision"]["current_hypothesis"]["name"] for event in final["events"] if event.get("kind") == "reasoning-decision"] == ["decision-a", "decision-b"]
    assert final["experiment_assessments"][1]["recommended_action"] == "proceed"
    assert final["experiment_assessments"][1]["duplicate"] is False


def test_after_verify_terminal_route_regressions(tmp_path: Path) -> None:
    state = initial_workflow_state({"id": "routes", "title": "Routes", "category": "misc"}, run_dir=tmp_path / "run", max_iterations=3)

    state["solved"] = True
    assert after_verify(state) == "finish_run"

    state["solved"] = False
    state["paused"] = True
    assert after_verify(state) == "human_review"

    state["paused"] = False
    state["failure_reason"] = "terminal"
    assert after_verify(state) == "fail_run"

    state["failure_reason"] = None
    state["unknowns"] = ["still unknown"]
    assert after_verify(state) == "reason_about_challenge"
