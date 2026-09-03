"""Import coverage for the experimental LangGraph/PydanticAI scaffold."""

from ctf_agent.graph import WorkflowState, build_workflow
from ctf_agent.pydantic_agent.agent import build_workflow_agent, llm_environment
from ctf_agent.pydantic_agent.models import ExperimentPlan, SolverDecision
from ctf_agent.pydantic_agent.tools import ToolDependencies


def test_experimental_agent_core_imports_and_graph_compiles():
    assert WorkflowState.__name__ == "WorkflowState"
    assert SolverDecision.__name__ == "SolverDecision"
    assert ExperimentPlan.__name__ == "ExperimentPlan"
    assert ToolDependencies.__name__ == "ToolDependencies"
    assert callable(build_workflow_agent)
    assert build_workflow() is not None


def test_llm_environment_requires_environment_only_settings():
    values = {"OPENAI_API_KEY": "test-key", "OPENAI_BASE_URL": "https://llm.example/v1", "OPENAI_MODEL": "test-model"}
    assert llm_environment(values)["OPENAI_MODEL"] == "test-model"
