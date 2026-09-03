# Security Model

This project is a local-first CTF solving workbench for authorized CTF events, local labs, competition infrastructure, and benchmarks. It is not a general autonomous offensive security system and should not be pointed at real targets without authorization.

## Boundaries

- Challenge files and high-churn run data stay under `~/ctf-workspace` by default.
- Windows GUI tooling receives files only through the explicit artifact export directory, `~/ctf-artifacts`.
- Public or third-party network activity is off by default for Docker runs.
- Flag submission is dry-run by default. Real CTFd submission requires profile config, CLI/UI intent, and a confirmation string.

## Process Execution

All planned commands flow through an `Executor` implementation:

- `LocalExecutor` runs through `/bin/bash` for CTF ergonomics, but first verifies that `cwd` is inside the configured workspace.
- `DockerExecutor` mounts only the workspace at `/workspace`, applies timeout, memory, CPU, and network settings, and records the Docker profile in trace metadata.
- Both executors call `validate_command_safety` before running a command.

The destructive command policy denies privileged/system operations and refuses destructive file operations unless every target resolves inside the workspace and does not target the workspace root. Shell-expanded destructive targets such as `$HOME/...`, absolute paths outside the workspace, `..` escapes, and `dd of=/outside` are blocked.

## Docker Network Policy

Docker network defaults to `none`.

Setting `sandbox.network: bridge` or another network is not enough by itself. The effective Docker network remains `none` unless:

- `sandbox.allow_network: true` or `sandbox.allow_challenge_network: true` is set in config or environment, and
- the challenge has an explicit `connection`, or the challenge category is `web`.

Every run records a `network-authorization` trace event with the requested network, effective network, whether it was allowed, and the authorization source (`challenge.connection`, `challenge.category=web`, or denial reason).

`LocalExecutor` cannot provide network namespace isolation. It records the same authorization decision for auditability, and LLM-driven network-capable commands are constrained by the command risk classifier.

## Challenge Connection Authorization

`Challenge.connection` is treated as the user/platform-provided authorization boundary for remote CTF services. CTFd imports preserve `connection_info`/`connection` in the challenge model. The trace records why network access was allowed or denied for the executor.

## Secrets

Tokens and API keys must come from environment variables or local ignored config files. OpenAI-compatible LLM connection settings are stricter: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL` are read only from environment variables, never YAML config.

- Environment variables such as `OPENAI_API_KEY`, `CTF_AGENT_CTFD_TOKEN`, and `CTF_AGENT_CTFD_<PROFILE>_TOKEN`.
- Ignored local config files such as `*.local.yaml`, `*private*.yaml`, `*secret*.yaml`, or `configs/ctfd*.yaml` for non-LLM platform secrets.

Plaintext secrets in ordinary config files such as `configs/default.yaml` are rejected by config loading. LLM fields `llm.api_key`, `llm.base_url`, and `llm.model` are rejected in every YAML config. `.gitignore` excludes common local secret config names.

Trace output is centrally redacted by `TraceStore`: sensitive keys (`token`, `api_key`, `authorization`, `secret`, `password`, etc.), known secret environment values, bearer tokens, and token assignments are replaced with `<redacted>`.

Raw command-output artifacts may contain challenge data or tool output and should be treated as sensitive local files.

## Network Requests

CTFd calls use explicit configured URLs and tokens. OpenAI-compatible LLM calls use environment-provided model settings and tokens. CTFd transport retries transient network errors and rate limits. These integrations are intended for authorized competition platforms and user-configured model endpoints only.

## Submission Safety

CTFd real submission requires all gates:

- profile `submit_enabled: true`;
- command/API request sets submit intent;
- exact confirmation string `SUBMIT <profile> <challenge_id>`.

The generic `ctf-agent submit` path does not pass a CTFd confirmation string, so it cannot bypass the dedicated `ctf-agent ctfd submit ... --confirm ...` safety path.

## Limitations

- `LocalExecutor` cannot isolate host network or filesystem effects beyond workspace path checks before command execution.
- Shell parsing is conservative but not a formal shell interpreter. High-risk automation should prefer Docker with `network=none`.
- Artifact files are not automatically scrubbed; only trace JSONL is redacted.
- The agent assumes challenge metadata and CTFd profiles are user-authorized inputs.
