from collections import Counter

from ctf_agent.agents import CryptoAgent, ForensicsAgent, PwnAgent, RevAgent, WebAgent
from ctf_agent.core.models import Challenge
from ctf_agent.core.orchestrator import Orchestrator
from ctf_agent.core.reporter import Reporter
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.evals import LocalBenchmark
from ctf_agent.platforms.local import LocalPlatformAdapter

from tests.test_multi_agent import make_context


def test_each_specialist_emits_structured_triage_metadata(tmp_path):
    cases = [
        (PwnAgent(), Challenge(id="p", title="Pwn", category="pwn", description="overflow", files=["chall"], connection="nc host 31337")),
        (RevAgent(), Challenge(id="r", title="Rev", category="rev", description="ELF reversing", files=["revbin"])),
        (CryptoAgent(), Challenge(id="c", title="RSA", category="crypto", description="rsa base64 xor lcg", files=["cipher.txt"])),
        (WebAgent(), Challenge(id="w", title="Web", category="web", description="form fuzz", files=["index.html"])),
        (ForensicsAgent(), Challenge(id="f", title="Image", category="forensics", description="png metadata", files=["image.png"])),
    ]
    payloads = {
        "chall": b"\x7fELFflag{dummy}",
        "revbin": b"\x7fELFmain",
        "cipher.txt": b"base64 ZmxhZ3tkdW1teX0=",
        "index.html": b"<form><input name=q></form>",
        "image.png": b"\x89PNG\r\n\x1a\ncomment",
    }
    for agent, challenge in cases:
        context = make_context(tmp_path / challenge.id, challenge)
        for name, content in payloads.items():
            if name in challenge.files:
                (context.layout.work_dir / name).write_bytes(content)
        plan = agent.run(context)
        assert plan.metadata["hypothesis"]
        assert plan.metadata["evidence"]
        assert plan.metadata["next_commands"]
        assert plan.commands
        trace = context.layout.trace_path.read_text(encoding="utf-8")
        assert "specialist-triage" in trace
        assert context.message_bus.by_kind("hypothesis")


def test_pwn_pipeline_writes_templates_and_report_includes_triage(tmp_path):
    challenge_dir = tmp_path / "pwncase"
    challenge_dir.mkdir()
    (challenge_dir / "challenge.yaml").write_text(
        "id: pwncase\n"
        "title: Pwn Case\n"
        "category: pwn\n"
        "description: overflow ret2win\n"
        "connection: nc pwn.local 31337\n"
        "files:\n"
        "  - chall\n"
        "metadata:\n"
        "  expected_flag: flag{pwn_report}\n",
        encoding="utf-8",
    )
    (challenge_dir / "chall").write_bytes(b"\x7fELF\x02\x01flag{pwn_report}\n")
    config = {"workspace_dir": str(tmp_path / "workspace"), "sandbox": {"engine": "local", "timeout_seconds": 10}, "memory": {"enabled": False, "auto_learn": False}}
    adapter = LocalPlatformAdapter(challenge_dir)
    result = Orchestrator(config, executor_name="local", brain="fallback", mode="specialist", max_steps=30).solve(adapter.get_challenge(str(challenge_dir)), adapter=adapter)
    assert result.solved is True
    assert (result.run_dir / "work" / "solve.py").exists()
    assert (result.run_dir / "work" / "gdb-notes.md").exists()
    writeup = Reporter(tmp_path / "workspace").generate(result.run_dir).read_text(encoding="utf-8")
    assert "## Specialist Triage" in writeup
    assert "Pwn triage" in writeup
    assert "solve.py" in writeup


def test_local_eval_dataset_covers_all_specialist_categories():
    dataset = LocalBenchmark("evals/datasets/local").list_challenges()
    counts = Counter(item.challenge.category for item in dataset)
    for category in ("pwn", "rev", "crypto", "web", "forensics"):
        assert counts[category] >= 2



def test_web_specialist_adds_php_analysis_commands(tmp_path):
    challenge = Challenge(id='phpweb', title='PHP Web', category='web', files=['index.php'])
    context = make_context(tmp_path / 'phpweb', challenge)
    (context.layout.work_dir / 'index.php').write_text("<?php include($_GET['page'] . '.php');", encoding='utf-8')
    plan = WebAgent().run(context)
    strategies = [command.metadata.get('strategy') for command in plan.commands]
    assert 'php-source-analysis' in strategies
    assert 'php-lfi-local-replay' in strategies


def test_php_lfi_benchmark_solves_without_network(tmp_path):
    dataset = LocalBenchmark('evals/datasets/local')
    item = next(item for item in dataset.list_challenges() if item.challenge.id == 'web-php-lfi-type-juggle')
    result = Orchestrator(
        {'workspace_dir': str(tmp_path / 'workspace'), 'sandbox': {'engine': 'local', 'timeout_seconds': 10}, 'memory': {'enabled': False}},
        executor_name='local',
        brain='fallback',
        mode='specialist',
        max_steps=12,
    ).solve(item.challenge, adapter=item.adapter)
    assert result.solved is True
    assert result.flags == ['flag{php_lfi_type_juggle}']
    assert result.metadata['execution']['results']
