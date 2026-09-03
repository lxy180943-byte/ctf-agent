from ctf_agent.graph.nodes import execute_experiment
from ctf_agent.graph.state import initial_workflow_state

def _plan():
    return {"goal":"read","action_type":"read_file","action_input":{"type":"read_file","path":"work/a.txt"},"expected_signal":"x","failure_signal":"y","risk":"low","rollback":"none"}

def test_execute_experiment_validates_plan_at_entry(tmp_path):
    state=initial_workflow_state({"id":"x","title":"x","category":"misc"},run_dir=tmp_path)
    state["experiments"]=[{"id":"e1","safety_checked":True,"plan":_plan(),"action_type":"read_file"}]
    result=execute_experiment(state)
    assert not any(item.get("source")=="experiment-validation" for item in result.get("observations",[]))

def test_execute_experiment_returns_structured_validation_failure(tmp_path):
    state=initial_workflow_state({"id":"x","title":"x","category":"misc"},run_dir=tmp_path)
    state["experiments"]=[{"id":"e1","safety_checked":True,"plan":{"action_type":"read_file"},"action_type":"read_file"}]
    result=execute_experiment(state)
    assert result["failed_actions"] and result["observations"][0]["source"]=="experiment-validation" and result["solved"] is False
