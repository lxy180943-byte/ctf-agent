from pathlib import Path

def test_planner_prompt_workflow_snapshot():
    prompt = (Path(__file__).resolve().parents[1] / "prompts" / "planner.md").read_text(encoding="utf-8")
    required = ["GPT is the primary reasoner", "Challenge JSON:", "Recent trace JSON:", "Structured observations JSON:", "Relevant skill notes JSON:", "Tool registry JSON:", "Memory JSON:", "hypothesis", "evidence_used", "uncertainty", "next_actions", "The `next_actions` array must contain 1 to 3 actions", "When PHP source is present, first identify parameters", "When an LFI or dynamic include is present, first establish a read primitive"]
    assert all(marker in prompt for marker in required)
    assert prompt.count("{{structured_observations_json}}") == 1
    assert prompt.count("{{relevant_skill_notes_json}}") == 1
