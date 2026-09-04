# CTF Agent Maturity Report

- generated_at: `2026-09-04T03:55:03.062721Z`
- level: `workflow-ready`
- levels: `scaffold, workflow-ready, competition-assistant, autonomous-baseline, mature`

## LLM

- provider: `none`
- model: `None`
- base_url: `None`
- timeout_seconds: `60`
- api_key_present: `False`
- real_provider: `False`
- ok: `True`
- recommendation: `LLM disabled; set CTF_AGENT_LLM_PROVIDER=openai and OPENAI_API_KEY/OPENAI_MODEL to enable GPT API use.`

## Tools

- total: `25`
- available: `13`
- available_ratio: `0.52`
- missing: `12`
- missing_tools: `crypto/RsaCtfTool, crypto/sage, crypto/z3, forensics/exiftool, forensics/foremost, forensics/zsteg, generic/rg, pwn/pwntools, rev/angr, rev/radare2, web/ffuf, web/playwright`
- note: `Install RsaCtfTool in a dedicated CTF tools environment.`
- note: `sudo apt install sagemath`
- note: `python -m pip install z3-solver`
- note: `sudo apt install libimage-exiftool-perl`
- note: `sudo apt install foremost`
- note: `gem install zsteg`
- note: `sudo apt install ripgrep`
- note: `python -m pip install pwntools`
- note: `python -m pip install angr`
- note: `sudo apt install radare2`
- note: `sudo apt install ffuf`
- note: `python -m pip install playwright && python -m playwright install`

## Docker

- docker_available: `True`
- ok: `True`
- ready_profiles: `generic, pwn, web, crypto, rev, forensics`
- missing_profiles: `-`

## Benchmark

- summary_path: `/tmp/pytest-of-liuxinyue/pytest-38/test_eval_cli_runs_filters_and0/eval-output/eval_summary.json`
- dataset: `local`
- challenge_count: `2`
- solved_count: `2`
- pass_rate: `1.0`
- false_positive_rate: `0.0`
- verifier_false_positive: `0`
- weak_categories: `-`

## Memory

- enabled: `False`
- ok: `False`
- total_items: `397`
- traceable_ratio: `1.00`
- avg_confidence: `0.94`
- quality_score: `0.97`
- note: `memory is disabled in config`

## UI

- ok: `True`
- health_url: `http://127.0.0.1:45365/api/health`

## Safety

- ok: `True`
- dry_run_default: `True`
- allow_network_default: `False`
- llm_env_only: `True`
- trace_redaction: `True`

## Missing To Mature

- Configure OPENAI_API_KEY, OPENAI_BASE_URL, and OPENAI_MODEL for a real GPT/Codex provider.
- Raise tool availability from 0.52 to at least 0.90.
- Improve memory quality and traceability.

## Notes

- Current level is workflow-ready; the report is intentionally conservative.
- No real LLM provider is configured yet.
- UI health endpoint is reachable locally.
- Dry-run submission and redaction policy are in the safe default state.
