# LLM Setup

ctf-agent uses graph as the default solve, resume, and eval brain. Graph mode runs LangGraph plus PydanticAI, backed by a configured GPT/Codex-compatible provider. Provider settings are environment-only. If graph provider configuration is missing or invalid, graph mode fails clearly and never falls back to deterministic solving. Use --brain fallback explicitly for offline deterministic compatibility.

## Environment Variables

Set these in your shell, secret manager, or local dotenv loader:

```bash
export CTF_AGENT_LLM_PROVIDER=openai
export OPENAI_API_KEY=replace-with-your-openai-api-key
export OPENAI_MODEL=replace-with-model-name
# Optional for non-default OpenAI-compatible endpoints:
export OPENAI_BASE_URL=replace-with-openai-compatible-base-url
```

For the official OpenAI API, `OPENAI_BASE_URL` may be omitted; the provider uses `https://api.openai.com/v1`. For other compatible endpoints, set `CTF_AGENT_LLM_PROVIDER=openai-compatible` and provide `OPENAI_BASE_URL`.

## What Must Not Go In YAML

Do not put OpenAI connection settings in `configs/default.yaml`, private YAML profiles, README snippets, trace fixtures, or test snapshots. The config loader rejects these YAML fields everywhere:

```yaml
llm:
  api_key: ...
  base_url: ...
  model: ...
```

Use `.env.example` only as a placeholder template. The real `.env` file is ignored by git.

## Brain Modes

- graph: default production mode. Requires PydanticAI provider environment variables and passes through LangGraph checkpoint/resume.
- fallback: explicit offline deterministic mode for local plumbing and smoke tests.
- llm and hybrid: deprecated legacy compatibility modes. The CLI accepts them for now and prints a deprecation warning.

Examples:

    ctf-agent doctor llm
    ctf-agent solve examples/challenge1 --executor local
    ctf-agent solve examples/challenge1 --brain fallback --executor local
    ctf-agent eval ./evals/datasets/local --brain fallback --executor local

## Doctor Check

Run the LLM doctor before solving with GPT:

```bash
ctf-agent doctor llm
ctf-agent doctor llm --json
```

The report shows provider, model, base URL, timeout, and whether a key is present. It never prints the API key value. Missing settings are reported by environment variable name, for example `OPENAI_API_KEY`.

## Redaction

Trace events and structured metadata are centrally redacted before being written. Sensitive keys such as `api_key`, authorization headers, bearer tokens, token assignments, and known secret environment values including `OPENAI_API_KEY` are replaced with `<redacted>`. Provider HTTP errors are also scrubbed before they can enter trace or logs.
