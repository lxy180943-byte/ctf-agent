import json

from ctf_agent.core.logging import JsonlTraceWriter, trace_from_config


def test_jsonl_trace_writer_emits_one_event(tmp_path):
    path = tmp_path / "trace.jsonl"
    writer = JsonlTraceWriter(path)
    writer.emit("unit_test", value=42)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == "unit_test"
    assert event["payload"]["value"] == 42


def test_trace_from_config_can_be_disabled(tmp_path):
    path = tmp_path / "disabled.jsonl"
    writer = trace_from_config({"logging": {"trace_enabled": False, "trace_path": str(path)}})
    writer.emit("ignored")
    assert not path.exists()
