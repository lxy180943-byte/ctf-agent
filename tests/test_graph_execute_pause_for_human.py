from ctf_agent.graph.nodes import NodeRuntime, bind_runtime, clear_runtime, execute_experiment
from ctf_agent.graph.state import initial_workflow_state, restore_workflow_state, workflow_state_to_json


def test_pause_for_human_persists_resumable_context(tmp_path):
    state = initial_workflow_state({"id": "pause", "title": "pause", "category": "misc"}, run_dir=tmp_path)
    state["current_hypothesis"] = "need authorization"
    state["iteration"] = 3
    state["experiments"] = [{"id": "e-pause", "safety_checked": True, "action_type": "pause", "plan": {"goal": "Await target clarification", "action_type": "pause", "action_input": {"type": "pause", "reason": "Provide the authorized local target."}, "expected_signal": "human response", "failure_signal": "no response", "risk": "low", "rollback": "none"}}]
    bind_runtime(tmp_path, NodeRuntime(tools=object()))
    try:
        result = execute_experiment(state)
    finally:
        clear_runtime(tmp_path)
    assert result["paused"] is True
    assert result["pause_reason"] == "Provide the authorized local target."
    assert result["next_goal"] == "Await target clarification"
    assert result["observations"][0]["requested_input"] == "Provide the authorized local target."
    assert result["tool_calls"][0]["status"] == "paused"
    assert state["solved"] is False
    merged = {**state, **result}
    restored = restore_workflow_state(workflow_state_to_json(merged))
    assert restored["paused"] is True
    assert restored["pause_reason"] == "Provide the authorized local target."
