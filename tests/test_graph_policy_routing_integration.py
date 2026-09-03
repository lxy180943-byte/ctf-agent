from __future__ import annotations

from pathlib import Path

from ctf_agent.agents import AgentContext
from ctf_agent.core.models import Challenge
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.graph.builder import build_workflow
from ctf_agent.graph.nodes import NodeRuntime, bind_runtime, clear_runtime
from ctf_agent.graph.state import initial_workflow_state
from ctf_agent.pydantic_agent.tools import ToolDependencies
from ctf_agent.sandbox import LocalExecutor
from ctf_agent.tools import default_registry


def _read_plan(path: str = "a.txt") -> dict:
    return {
        "goal": "Read local source",
        "action_type": "read_file",
        "action_input": {"type": "read_file", "path": path},
        "expected_signal": "known marker",
        "failure_signal": "missing",
        "risk": "low",
        "rollback": "none",
    }


def _decision(plan: dict) -> dict:
    return {
        "current_hypothesis": {
            "name": "inspect local file",
            "claim": "The local attachment may contain useful evidence.",
            "evidence_for": [],
            "evidence_against": [],
            "confidence": 0.4,
            "falsification_test": "Read the proposed file and check for the expected marker.",
        },
        "confirmed_facts": [],
        "unknowns": ["entry point"],
        "candidate_chains": [["inspect local file"]],
        "selected_experiment": plan,
        "next_action": "execute_selected_experiment",
        "need_human": False,
        "stop_reason": None,
    }


def _context(tmp_path: Path, challenge: Challenge) -> tuple[AgentContext, WorkspaceManager]:
    manager = WorkspaceManager(tmp_path / "workspace")
    state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    trace = manager.trace_store_for(challenge.id)
    context = AgentContext(
        state=state,
        layout=layout,
        trace_store=trace,
        executor=LocalExecutor(manager.workspace_root, trace_store=trace, challenge_id=challenge.id),
        tool_registry=default_registry(),
        config={},
        max_steps=4,
        timeout=5,
    )
    return context, manager


def _invoke(state: dict, *, thread_id: str, max_tool_calls: int | None = None) -> dict:
    workflow = build_workflow(max_tool_calls=max_tool_calls)
    return workflow.invoke(state, config={"configurable": {"thread_id": thread_id}, "recursion_limit": 25})


def test_policy_replan_route_returns_to_reason_without_execution_or_evidence_pollution(tmp_path: Path) -> None:
    challenge = Challenge(id="policy-replan", title="Policy replan", category="web", files=["a.txt"])
    context, _manager = _context(tmp_path, challenge)
    plan = _read_plan("a.txt")
    state = initial_workflow_state(challenge, run_dir=context.layout.challenge_dir, max_iterations=4)
    state["experiments"] = [{"id": "prior-exp", "action_type": "read_file", "plan": plan, "completed": True}]
    state["tool_calls"] = [
        {
            "id": "prior-call",
            "experiment_id": "prior-exp",
            "action_type": "read_file",
            "action_input": plan["action_input"],
            "status": "executed",
            "failure_signal_matched": False,
        }
    ]
    state["observations"] = [{"source": "read_file", "evidence": {"path": "a.txt", "body_excerpt": "old evidence"}, "ok": True}]
    calls = {"reasoner": 0}

    def reasoner(_workflow_state):
        calls["reasoner"] += 1
        if calls["reasoner"] == 1:
            return _decision(plan)
        raise RuntimeError("stop after policy replan returned to reasoner")

    bind_runtime(context.layout.challenge_dir, NodeRuntime(reasoner=reasoner))
    try:
        final = _invoke(state, thread_id="policy-replan")
    finally:
        clear_runtime(context.layout.challenge_dir)

    trace = context.layout.trace_path.read_text(encoding="utf-8")
    assert calls["reasoner"] == 2
    assert final["phase"] == "failed"
    assert final["replan_required"] is True
    assert len(final["tool_calls"]) == 1
    assert final["tool_calls"][0]["id"] == "prior-call"
    assert final["evidence_deltas"] == []
    assert any(item.get("source") == "experiment_policy" for item in final["observations"])
    assert "execute_experiment" not in trace
    assert "summarize_observation" not in trace
    assert "update_hypotheses" not in trace
    assert "verify_candidates" not in trace


def test_normal_policy_proceed_route_reaches_execute_experiment(tmp_path: Path) -> None:
    challenge = Challenge(id="policy-normal", title="Policy normal", category="web", files=["a.txt"])
    context, _manager = _context(tmp_path, challenge)
    (context.layout.work_dir / "a.txt").write_text("known marker", encoding="utf-8")
    state = initial_workflow_state(challenge, run_dir=context.layout.challenge_dir, max_iterations=2)
    plan = _read_plan("a.txt")

    bind_runtime(context.layout.challenge_dir, NodeRuntime(tools=ToolDependencies(context=context), reasoner=lambda _state: _decision(plan)))
    try:
        final = _invoke(state, thread_id="policy-normal", max_tool_calls=1)
    finally:
        clear_runtime(context.layout.challenge_dir)

    trace = context.layout.trace_path.read_text(encoding="utf-8")
    assert final["tool_calls"]
    assert final["tool_calls"][0]["action_type"] == "read_file"
    assert final["tool_calls"][0]["status"] == "executed"
    assert final["evidence_deltas"]
    assert "execute_experiment" in trace
    assert "summarize_observation" in trace
    assert "verify_candidates" in trace
