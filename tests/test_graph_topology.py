from ctf_agent.graph.builder import build_workflow
from ctf_agent.graph.edges import after_select_experiment, after_verify
from ctf_agent.graph.state import initial_workflow_state


def _state(tmp_path):
    return initial_workflow_state({"id": "topology", "title": "Topology", "category": "misc"}, run_dir=tmp_path / "run", max_iterations=3)


def test_workflow_has_required_nodes_and_checkpoint_support():
    graph = build_workflow()
    names = set(graph.get_graph().nodes)
    assert {"ingest_challenge", "collect_initial_evidence", "retrieve_skills", "retrieve_memory", "reason_about_challenge", "select_experiment", "execute_experiment", "summarize_observation", "update_hypotheses", "verify_candidates", "human_review", "finish_run", "fail_run"}.issubset(names)
    assert graph.checkpointer is not None


def test_verify_routes_have_bounded_exits(tmp_path):
    state = _state(tmp_path)
    state["solved"] = True
    assert after_verify(state) == "finish_run"
    state["solved"] = False
    state["paused"] = True
    assert after_verify(state) == "human_review"
    state["paused"] = False
    state["iteration"] = state["max_iterations"]
    assert after_verify(state) == "fail_run"


def test_verify_routes_to_next_experiment_or_reason_and_breaks_repetition(tmp_path):
    state = _state(tmp_path)
    state["unknowns"] = ["entry point"]
    assert after_verify(state) == "select_experiment"
    state["unknowns"] = []
    assert after_verify(state) == "reason_about_challenge"
    state["tool_calls"] = [{"action_type": "read_file"}] * 3
    assert after_verify(state, max_repeated_actions=3) == "fail_run"


def test_select_experiment_uses_conditional_policy_routes_in_compiled_graph():
    graph = build_workflow().get_graph()
    select_edges = [edge for edge in graph.edges if edge.source == "select_experiment"]

    assert not any(edge.target == "execute_experiment" and not edge.conditional for edge in select_edges)
    assert {edge.target for edge in select_edges if edge.conditional} == {
        "execute_experiment",
        "reason_about_challenge",
        "human_review",
        "fail_run",
    }


def test_after_select_experiment_router_unit_contract(tmp_path):
    state = _state(tmp_path)
    state["phase"] = "experiment-selected"
    state["selected_experiment"] = {"action_type": "read_file"}
    assert after_select_experiment(state) == "execute_experiment"

    state["replan_required"] = True
    assert after_select_experiment(state) == "reason_about_challenge"

    state["replan_required"] = False
    state["paused"] = True
    assert after_select_experiment(state) == "human_review"

    state["paused"] = False
    state["failure_reason"] = "terminal"
    assert after_select_experiment(state) == "fail_run"
