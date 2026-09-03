import json

from ctf_agent.agents import AgentContext, AgentMessageBus, CategoryClassifier, CriticAgent, PwnAgent, specialist_for_category
from ctf_agent.core.models import Challenge
from ctf_agent.core.orchestrator import Orchestrator
from ctf_agent.core.state import ChallengeRunState
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.platforms.local import LocalPlatformAdapter
from ctf_agent.sandbox import LocalExecutor
from ctf_agent.tools import ToolRegistry, ToolSpec, default_registry


def make_context(tmp_path, challenge, registry=None):
    manager = WorkspaceManager(tmp_path / "workspace")
    state = manager.init_state(challenge)
    return AgentContext(
        state=state,
        layout=manager.layout_for(challenge.id),
        trace_store=manager.trace_store_for(challenge.id),
        executor=LocalExecutor(tmp_path / "workspace"),
        tool_registry=registry or default_registry(),
        config={},
        max_steps=10,
        timeout=30,
        message_bus=AgentMessageBus(),
    )


def test_category_classifier_uses_description_and_connection():
    classifier = CategoryClassifier()
    web = classifier.classify(Challenge(id="w", title="Login", category="misc", description="Find the SQL injection", connection="https://ctf.example"))
    crypto = classifier.classify(Challenge(id="c", title="RSA", category="misc", description="Small modulus and prime leak"))
    assert web.category == "web"
    assert crypto.category == "crypto"
    assert web.scores["web"] > 0
    assert crypto.scores["crypto"] > 0


def test_category_classifier_uses_file_magic(tmp_path):
    challenge_dir = tmp_path / "rev"
    challenge_dir.mkdir()
    (challenge_dir / "chall").write_bytes(b"\x7fELF" + b"\x00" * 20)
    result = CategoryClassifier().classify(Challenge(id="rev", title="binary", category="misc", files=["chall"]), challenge_dir)
    assert result.category in {"pwn", "rev"}
    assert "magic ELF->pwn/rev" in result.evidence


def test_message_bus_collects_shared_items():
    bus = AgentMessageBus()
    bus.add_hypothesis("planner", "maybe strings")
    bus.add_observation("executor", "saw text")
    bus.add_failure("verifier", "no flag")
    assert len(bus.by_kind("hypothesis")) == 1
    assert bus.to_dict()["messages"][2]["kind"] == "failure_reason"


def test_specialist_selects_tools_through_registry(tmp_path):
    registry = ToolRegistry(
        [
            ToolSpec(name="checksec", category="pwn", description="", command_template="", required_bins=["checksec"]),
            ToolSpec(name="file", category="generic", description="", command_template="", required_bins=["file"]),
        ]
    )
    challenge = Challenge(id="pwn", title="Pwn", category="pwn", files=["chall"])
    context = make_context(tmp_path, challenge, registry=registry)
    plan = PwnAgent().run(context)
    selected = [tool["name"] for tool in plan.metadata["selected_tools"]]
    assert selected == ["checksec"]
    assert plan.metadata["source"] == "specialist"


def test_specialist_mode_solves_toy_challenge(tmp_path):
    challenge_dir = tmp_path / "challenge1"
    challenge_dir.mkdir()
    (challenge_dir / "challenge.yaml").write_text("title: Toy\ncategory: forensics\nfiles:\n  - prompt.txt\n", encoding="utf-8")
    (challenge_dir / "prompt.txt").write_text("flag{specialist_mode}\n", encoding="utf-8")
    adapter = LocalPlatformAdapter(challenge_dir)
    challenge = adapter.get_challenge(str(challenge_dir))
    orchestrator = Orchestrator(
        {"workspace_dir": str(tmp_path / "workspace"), "sandbox": {"engine": "local", "timeout_seconds": 10}},
        executor_name="local",
        brain="fallback",
        mode="specialist",
    )
    result = orchestrator.solve(challenge, adapter=adapter)
    assert result.solved is True
    assert result.flags == ["flag{specialist_mode}"]
    assert result.metadata["mode"] == "specialist"
    assert result.metadata["classification"]["category"] == "forensics"
    assert result.metadata["message_bus"]["messages"]


def test_critic_after_failures_mode_can_recover_with_alternative_strategy(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspace")
    challenge = Challenge(id="critic-toy", title="Critic Toy", category="misc", files=["missing.txt"])
    state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    layout.work_dir.mkdir(parents=True, exist_ok=True)
    (layout.work_dir / "secret.txt").write_text("flag{critic_recovered}\n", encoding="utf-8")
    manager.save_state(state)

    orchestrator = Orchestrator(
        {
            "workspace_dir": str(tmp_path / "workspace"),
            "sandbox": {"engine": "local", "timeout_seconds": 10},
            "orchestration": {"mode": "critic-after-failures", "critic_after_failures": 1},
        },
        executor_name="local",
        brain="fallback",
        mode="critic-after-failures",
        critic_after_failures=1,
        max_steps=4,
    )
    result = orchestrator.resume_from_run_dir(layout.challenge_dir)
    assert result.solved is True
    assert result.flags == ["flag{critic_recovered}"]
    assert result.metadata["message_bus"]["messages"]
    assert "critic-plan" in layout.trace_path.read_text(encoding="utf-8")


def test_critic_agent_plan_uses_failure_messages(tmp_path):
    challenge = Challenge(id="critic", title="Critic", category="misc")
    context = make_context(tmp_path, challenge)
    context.message_bus.add_failure("verifier", "no flag")
    plan = CriticAgent().run(context)
    assert plan.metadata["failure_count"] == 1
    assert "workspace flag scan" in plan.rationale


def test_specialist_factory_returns_category_agent():
    assert specialist_for_category("web").category == "web"
    assert specialist_for_category("unknown").category == "generic"
