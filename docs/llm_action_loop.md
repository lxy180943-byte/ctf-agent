# LLM Action Loop

The LLM decision path now uses a strict tool-action loop instead of free-form command lists.

## Action Schema

Each LLM response must be strict JSON with a non-empty `rationale` and 1 to 3 actions. Supported action types are:

- `run_command`: execute one shell command through the configured Executor.
- `read_file`: read a relative file from the challenge work directory or artifacts.
- `write_file`: write a helper file inside the work directory, or under `artifacts/`.
- `search_artifacts`: search workspace/artifact files for a literal pattern.
- `ask_verifier`: ask the verifier to extract and validate observed flag candidates.
- `finish`: end the run with an already observed and verifiable flag.
- `pause`: leave the run resumable.

Validation is implemented in `ctf_agent.llm.actions` with a custom schema validator, so no external JSON schema dependency is required.

## Hallucination Guard

The loop rejects actions that reference files outside the work/artifact roots, read missing files, or finish with a flag that has not appeared in challenge metadata, observations, trace-derived context, or verified candidates. Executor results are summarized back into the next prompt as observations; full output still lives in command-output artifacts.

## Command Risk

`ctf_agent.llm.risk` classifies commands as `low`, `medium`, `high`, or `refuse`. Privileged/system-destructive patterns are refused. Destructive file operations and unscoped network commands are blocked with `confirm_required` metadata instead of being executed.

## Orchestration

The interactive llm/hybrid action loop is a deprecated legacy compatibility path. The default production brain is graph; missing graph provider configuration fails clearly. The deterministic planner/executor/verifier path runs only when the user explicitly selects --brain fallback.
