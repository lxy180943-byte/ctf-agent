import pytest
from pydantic import ValidationError

from ctf_agent.pydantic_agent.models import ExperimentPlan


def _plan(action_type, action_input):
    return {"goal": "inspect", "action_type": action_type, "action_input": action_input, "expected_signal": "marker", "failure_signal": "no marker", "risk": "low", "rollback": "none"}


def test_valid_read_file_and_http_experiments_are_discriminated():
    read = ExperimentPlan.model_validate(_plan("read_file", {"type": "read_file", "path": "work/source.php"}))
    http = ExperimentPlan.model_validate(_plan("http_request", {"type": "http_request", "method": "GET", "url": "http://ctf.local/", "params": {"a": "1"}, "headers": {}, "body": None, "timeout": 20}))
    assert read.action_input.path == "work/source.php"
    assert http.action_input.method == "GET"


def test_action_type_mismatch_and_missing_required_inputs_fail():
    with pytest.raises(ValidationError): ExperimentPlan.model_validate(_plan("read_file", {"type": "inspect_binary", "path": "x"}))
    for action_type, action_input in [("run_command", {"type": "run_command"}), ("read_file", {"type": "read_file"}), ("http_request", {"type": "http_request"})]:
        with pytest.raises(ValidationError): ExperimentPlan.model_validate(_plan(action_type, action_input))


@pytest.mark.parametrize("action_input", [{"type": "read_file", "path": "/etc/passwd"}, {"type": "inspect_binary", "path": "C:\\temp\\x"}, {"type": "run_command", "command": "true", "timeout": 0}, {"type": "http_request", "url": "http://ctf.local", "timeout": 601}])
def test_absolute_paths_and_invalid_timeouts_fail(action_input):
    with pytest.raises(ValidationError): ExperimentPlan.model_validate(_plan(action_input["type"], action_input))
