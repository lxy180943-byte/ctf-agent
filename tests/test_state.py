import pytest

from ctf_agent.core.models import Challenge, FlagCandidate, Step
from ctf_agent.core.state import ChallengeRunState, ChallengeState, InvalidStateTransition


def sample_challenge() -> Challenge:
    return Challenge(id="web-1", title="Web 1", category="web")


def test_valid_state_transitions():
    state = ChallengeRunState(challenge=sample_challenge())
    state.transition_to(ChallengeState.ANALYZING)
    state.transition_to("running")
    state.transition_to(ChallengeState.VERIFYING)
    state.transition_to(ChallengeState.SOLVED)
    assert state.state is ChallengeState.SOLVED


def test_invalid_state_transition_from_solved():
    state = ChallengeRunState(challenge=sample_challenge(), state=ChallengeState.SOLVED)
    with pytest.raises(InvalidStateTransition):
        state.transition_to(ChallengeState.RUNNING)


def test_attempt_and_state_roundtrip():
    state = ChallengeRunState(challenge=sample_challenge())
    attempt = state.start_attempt()
    step = Step(agent="planner", action="plan")
    attempt.add_step(step)
    state.add_flag_candidate(FlagCandidate(value="flag{maybe}", source="unit"))

    restored = ChallengeRunState.from_dict(state.to_dict())
    assert restored.challenge.id == "web-1"
    assert restored.attempts[0].steps[0].action == "plan"
    assert restored.flag_candidates[0].submitted is False
