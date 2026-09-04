from __future__ import annotations

import copy
import json

from ctf_agent.graph import EvidenceDelta, derive_evidence_delta
from ctf_agent.graph.nodes import _reconciliation_signal


def _plan(action_type: str, action_input: dict, *, expected="expected", failure="failure"):
    return {
        "id": "exp-1",
        "safety_checked": True,
        "action_type": action_type,
        "plan": {
            "goal": "collect real evidence",
            "action_type": action_type,
            "action_input": action_input,
            "expected_signal": expected,
            "failure_signal": failure,
            "risk": "low",
            "rollback": "none",
        },
        "hypothesis": "model guess must not become fact",
        "memory": "memory note must stay advisory",
        "skill_notes": ["skill note must stay advisory"],
        "candidate_chains": [["model chain"]],
    }


def _call(action_type: str, **extra):
    data = {
        "id": "call-1",
        "experiment_id": "exp-1",
        "action_type": action_type,
        "action_input": {"type": action_type},
        "status": "executed",
        "expected_signal": "expected",
        "failure_signal": "failure",
        "expected_signal_matched": True,
        "failure_signal_matched": False,
        "duration_seconds": 0.01,
        "artifact_paths": ["artifacts/out.txt"],
    }
    data.update(extra)
    return data


def _summaries(items):
    return json.dumps(items, sort_keys=True)


def test_read_file_observation_produces_file_facts_with_provenance():
    experiment = _plan("read_file", {"type": "read_file", "path": "work/app.php"})
    observation = {
        "source": "read_file",
        "ok": True,
        "observation_index": 3,
        "evidence": {
            "path": "work/app.php",
            "bytes_read": 128,
            "truncated": False,
            "body_excerpt": "<?php echo expected; ?> Authorization: Bearer secret-value",
        },
    }
    call = _call("read_file", action_input={"type": "read_file", "path": "work/app.php"})

    delta = derive_evidence_delta(experiment, observation, call)

    assert isinstance(delta, EvidenceDelta)
    assert delta.provenance.source_type == "read_file"
    assert delta.provenance.source_id == "exp-1"
    assert delta.provenance.tool_call_id == "call-1"
    assert delta.provenance.artifact_path == "artifacts/out.txt"
    assert delta.provenance.observation_index == 3
    facts = _summaries(delta.confirmed_facts)
    assert "read_file_path" in facts
    assert "read_file_bytes" in facts
    assert "work/app.php" in facts
    assert "secret-value" not in facts
    assert "Authorization" not in facts
    assert "Bearer" not in facts
    assert all("provenance" in item for item in delta.confirmed_facts)


def test_php_http_observation_produces_parameter_sink_blacklist_constraints():
    experiment = _plan("http_request", {"type": "http_request", "url": "http://challenge.local"})
    observation = {
        "source": "http_request",
        "ok": True,
        "evidence": {
            "status": 200,
            "headers": {"content-type": "text/html", "Authorization": "Bearer header-secret"},
            "forms": [{"method": "get", "inputs": [{"name": "page"}]}],
            "links": ["/source"],
            "request": {"method": "GET", "url": "http://challenge.local", "parameter_names": ["page"], "header_names": ["X-Test"]},
            "php_analysis": {
                "parameters": [{"superglobal": "GET", "name": "page"}],
                "sinks": ["include"],
                "guards": [{"kind": "loose-comparison", "expr": "$a == $b"}],
                "blacklist": ["/flag/"],
            },
        },
    }
    call = _call("http_request")

    delta = derive_evidence_delta(experiment, observation, call)

    facts = _summaries(delta.confirmed_facts)
    constraints = _summaries(delta.constraints)
    assert "http_status" in facts
    assert "http_forms" in facts
    assert "php_parameter" in constraints
    assert "php_sink" in constraints
    assert "php_comparison" in constraints
    assert "php_blacklist" in constraints
    assert "http_network_scope" in constraints
    assert "header-secret" not in json.dumps(delta.model_dump(mode="json"))
    assert "Authorization" not in json.dumps(delta.model_dump(mode="json"))
    assert "Bearer" not in json.dumps(delta.model_dump(mode="json"))


def test_inspect_binary_observation_produces_binary_facts_and_protection_constraints():
    experiment = _plan("inspect_binary", {"type": "inspect_binary", "path": "work/chall"})
    observation = {
        "source": "inspect_binary",
        "ok": True,
        "evidence": {
            "path": "work/chall",
            "binary": {
                "format": "ELF",
                "file_type": "ELF 64-bit LSB pie executable",
                "arch": "x86-64",
                "protections": {"nx": True, "pie": True, "canary": False},
            },
        },
    }
    call = _call("inspect_binary", action_input={"type": "inspect_binary", "path": "work/chall"})

    delta = derive_evidence_delta(experiment, observation, call)

    facts = _summaries(delta.confirmed_facts)
    constraints = _summaries(delta.constraints)
    assert "binary_format" in facts
    assert "binary_file_type" in facts
    assert "binary_arch" in facts
    assert "binary_protection" in constraints
    assert "nx" in constraints
    assert "pie" in constraints
    assert "canary" in constraints


def test_allowed_low_risk_inspect_binary_success_does_not_emit_risk_block():
    experiment = _plan("inspect_binary", {"type": "inspect_binary", "path": "revbin"})
    observation = {
        "source": "inspect_binary",
        "ok": True,
        "evidence": {
            "path": "revbin",
            "exit_code": 0,
            "binary": {
                "format": "ELF",
                "file_type": "ELF 64-bit LSB *unknown arch 0x6c20* (SYSV)",
                "protections": {
                    "available": False,
                    "reason": "No checksec-style analyzer is registered for this profile.",
                },
            },
        },
    }
    call = _call(
        "inspect_binary",
        action_input={"type": "inspect_binary", "path": "revbin"},
        expected_signal="Binary inspection returns file identification and printable content.",
        expected_signal_matched=False,
        failure_signal_matched=False,
        risk_decision={"level": "low", "reason": "Workspace path guard passed."},
        status="executed",
    )

    delta = derive_evidence_delta(experiment, observation, call)
    signal = _reconciliation_signal(delta.model_dump(mode="json"), experiment, call)

    facts = _summaries(delta.confirmed_facts)
    anomalies = _summaries(delta.anomalies)
    assert "binary_format" in facts
    assert "binary_file_type" in facts
    assert "authorization_or_risk_block" not in anomalies
    assert signal["risk_block"] is False
    assert signal["failure_kind"] != "risk_block"


def test_explicit_risk_refusal_emits_authorization_or_risk_block():
    experiment = _plan("run_command", {"type": "run_command", "command": "dangerous"}, expected="ok")
    observation = {
        "source": "run_command",
        "ok": False,
        "error": "risk policy refused execution",
        "evidence": {"exit_code": None},
    }
    call = _call(
        "run_command",
        action_input={"type": "run_command", "command": "dangerous"},
        status="blocked",
        risk_decision={"level": "refuse", "reason": "blocked by policy"},
        expected_signal_matched=False,
    )

    delta = derive_evidence_delta(experiment, observation, call)
    signal = _reconciliation_signal(delta.model_dump(mode="json"), experiment, call)

    anomalies = _summaries(delta.anomalies)
    assert "authorization_or_risk_block" in anomalies
    assert signal["risk_block"] is True
    assert signal["failure_kind"] == "risk_block"


def test_allowed_success_without_expected_signal_is_not_risk_block():
    experiment = _plan("read_file", {"type": "read_file", "path": "note.txt"}, expected="needle")
    observation = {
        "source": "read_file",
        "ok": True,
        "evidence": {
            "path": "note.txt",
            "bytes_read": 32,
            "truncated": False,
            "body_excerpt": "ordinary evidence without the expected phrase",
        },
    }
    call = _call(
        "read_file",
        action_input={"type": "read_file", "path": "note.txt"},
        risk_decision={"level": "low", "reason": "allowed"},
        expected_signal="needle",
        expected_signal_matched=False,
        failure_signal_matched=False,
        status="executed",
    )

    delta = derive_evidence_delta(experiment, observation, call)
    signal = _reconciliation_signal(delta.model_dump(mode="json"), experiment, call)

    anomalies = _summaries(delta.anomalies)
    assert "expected_signal_missing" in anomalies
    assert "authorization_or_risk_block" not in anomalies
    assert signal["risk_block"] is False
    assert signal["failure_kind"] != "risk_block"


def test_timeout_blocked_and_failure_signal_produce_anomalies_and_constraints():
    experiment = _plan("http_request", {"type": "http_request", "url": "http://other.local"}, expected="needle", failure="denied")
    observation = {
        "source": "http_request",
        "ok": False,
        "authorization": {"allowed": False, "reason": "denied outside authorized target"},
        "evidence": {"status": 403, "exit_code": 124, "timed_out": True, "body_excerpt": "denied"},
        "error": "denied outside authorized target",
    }
    call = _call(
        "http_request",
        status="blocked",
        risk_decision={"level": "refuse", "reason": "blocked"},
        expected_signal="needle",
        failure_signal="denied",
        expected_signal_matched=False,
        failure_signal_matched=True,
    )

    delta = derive_evidence_delta(experiment, observation, call)

    anomalies = _summaries(delta.anomalies)
    constraints = _summaries(delta.constraints)
    assert "tool_failure" in anomalies
    assert "nonzero_exit" in anomalies
    assert "timeout" in anomalies
    assert "expected_signal_missing" in anomalies
    assert "failure_signal_matched" in anomalies
    assert "http_unexpected_status" in anomalies
    assert "authorization_or_risk_block" in anomalies
    assert "tool_blocked" in constraints
    assert "failure_signal" in constraints


def test_advisory_text_and_unverified_flag_do_not_enter_confirmed_facts():
    experiment = _plan("read_file", {"type": "read_file", "path": "work/a.txt"})
    observation = {
        "source": "read_file",
        "ok": True,
        "hypothesis": "The answer is flag{model_guess}",
        "memory": "memory claims flag{memory_guess}",
        "skill_notes": ["skill says maybe flag{skill_guess}"],
        "evidence": {"path": "work/a.txt", "bytes_read": 12, "body_excerpt": "ordinary text"},
        "verified_candidates": [{"value": "flag{unverified}", "source": "model", "verified": False}],
    }
    call = _call("read_file")

    delta = derive_evidence_delta(experiment, observation, call)

    confirmed = _summaries(delta.confirmed_facts)
    artifacts = _summaries(delta.candidate_artifacts)
    assert "flag{model_guess}" not in confirmed
    assert "flag{memory_guess}" not in confirmed
    assert "flag{skill_guess}" not in confirmed
    assert "flag{unverified}" not in confirmed
    assert "flag{unverified}" not in artifacts
    assert any("Ignored advisory" in note for note in delta.extraction_notes)
    assert any("Ignored unverified" in note for note in delta.extraction_notes)


def test_verifier_verified_candidate_is_allowed_but_unverified_candidate_is_not():
    experiment = _plan("ask_verifier", {"type": "ask_verifier"})
    observation = {
        "source": "verifier",
        "ok": True,
        "evidence": {
            "verified_count": 1,
            "candidates": [
                {"value": "verified-candidate", "source": "verifier", "verified": True},
                {"value": "flag{unverified}", "source": "model", "verified": False},
            ],
        },
    }
    call = _call("ask_verifier")

    delta = derive_evidence_delta(experiment, observation, call)

    confirmed = _summaries(delta.confirmed_facts)
    assert "verified_candidate" in confirmed
    assert "verified-candidate" in confirmed
    assert "flag{unverified}" not in confirmed


def test_delta_is_json_serializable_stable_and_has_no_side_effects():
    experiment = _plan("search_artifacts", {"type": "search_artifacts", "pattern": "needle"})
    observation = {
        "source": "search_artifacts",
        "ok": True,
        "evidence": {
            "pattern": "needle",
            "match_count": 1,
            "matches": [{"path": "work/a.txt", "line": 4, "raw": "token=secret-value"}],
        },
    }
    call = _call("search_artifacts", action_input={"type": "search_artifacts", "pattern": "needle"})
    original = (copy.deepcopy(experiment), copy.deepcopy(observation), copy.deepcopy(call))

    first = derive_evidence_delta(experiment, observation, call)
    second = derive_evidence_delta(experiment, observation, call)
    encoded = json.dumps(first.model_dump(mode="json"), sort_keys=True)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert (experiment, observation, call) == original
    assert json.loads(encoded)["provenance"]["source_type"] == "search_artifacts"
    assert "search_match" in encoded
    assert "secret-value" not in encoded
    assert "token" not in encoded
