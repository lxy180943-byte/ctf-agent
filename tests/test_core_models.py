import json

from ctf_agent.core.models import Artifact, Challenge, FlagCandidate, Observation, Step


def test_challenge_serialization_roundtrip():
    challenge = Challenge(
        id="baby-rev",
        title="Baby Rev",
        category="rev",
        description="Find the flag.",
        files=["chall"],
        connection="nc example.invalid 31337",
        hints=["strings may help"],
        flag_regex=r"flag\{.*\}",
        metadata={"points": 100},
    )
    restored = Challenge.from_dict(json.loads(json.dumps(challenge.to_dict())))
    assert restored == challenge


def test_step_serialization_includes_observations_and_artifacts():
    step = Step(
        agent="executor",
        action="triage-file",
        command=["file", "chall"],
        observations=[Observation(summary="ELF binary", raw="ELF 64-bit")],
        artifacts=[Artifact(path="artifacts/file.txt", kind="report")],
        exit_code=0,
    )
    restored = Step.from_dict(step.to_dict())
    assert restored.agent == "executor"
    assert restored.observations[0].summary == "ELF binary"
    assert restored.artifacts[0].kind == "report"


def test_flag_candidate_defaults_are_safe():
    candidate = FlagCandidate(value="flag{demo}", source="regex")
    assert candidate.verified is False
    assert candidate.submitted is False
    assert candidate.confidence == 0.0
