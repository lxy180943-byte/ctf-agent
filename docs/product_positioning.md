# Product Positioning: Codex/GPT-Driven CTF Workflow Agent

## Positioning

`ctf-agent` is a Codex/GPT-driven workflow agent for authorized CTF challenges, local labs, and benchmark environments.

The primary production reasoning path is LangGraph plus PydanticAI backed by GPT/Codex. This project should not try to become a hand-written rule engine for solving every CTF category. Its durable value is the local workflow substrate around the model:

- challenge ingestion and workspace preparation
- prompt/context assembly
- tool discovery and safe command execution
- trace, artifact capture, resume, and reporting
- verification and dry-run submission flow
- benchmark evaluation and regression tracking
- local memory with quality controls
- UI for inspecting runs, traces, artifacts, and model decisions

In short: GPT/Codex thinks; `ctf-agent` observes, executes, records, verifies, and evaluates.

## Core Principles

1. LangGraph/PydanticAI is the main brain.

   Planning, hypothesis generation, experiment selection, checkpointed resume, and cross-step reasoning happen in the graph workflow. Legacy llm/hybrid modes remain compatibility paths only.

2. The agent runtime is the workflow layer.

   The codebase should make the model effective by supplying compact context, bounded tools, reliable observations, and resumable state. It should not encode a growing catalog of handcrafted solve playbooks as the default path.

3. Specialists are context providers, not primary solvers.

   Category specialists may recommend tools, summarize artifacts, extract domain signals, and provide fallback triage. They should not own the main route or emit long deterministic solve scripts unless the user explicitly selects fallback mode.

4. Rule libraries should be small and observational.

   Static analyzers and pattern detectors are useful when they convert raw outputs into structured evidence for GPT/Codex. They should avoid becoming complete exploitation engines.

5. Verification is conservative.

   Flags must be exactly observed or produced by a controlled local reproduction. Real submissions stay dry-run by default and require explicit confirmation.

6. Network behavior is scoped.

   Network tools may run only against challenge-provided, authorized endpoints or local lab/benchmark targets, with bounded request volume and traceable authorization metadata.

7. Memory is advisory.

   Memory should provide prior context with confidence and provenance. It must not silently steer the solve loop with low-quality failure routes.

## Product Non-Goals

- No autonomous public-target scanning.
- No attempt to replace GPT/Codex with a complete handwritten CTF solver.
- No broad exploit library that fires payloads by default.
- No real flag submission unless explicitly requested and confirmed.
- No hidden network expansion beyond the challenge boundary.

## Intended Architecture

```text
Challenge source
  -> PlatformAdapter
  -> WorkspaceManager
  -> Context providers
       - classifier
       - tool registry
       - static summarizers
       - memory retrieval
       - specialist recommendations
  -> LangGraph workflow
       - PydanticAI/GPT decides the next graph experiment
       - strict typed tool protocol
       - hallucination/path/risk guards
  -> Executor
       - local/docker bounded execution
       - artifacts and stdout/stderr capture
  -> Observation summarizers
       - compact raw output
       - structured domain facts
  -> Verifier
       - exact candidate extraction
       - conservative confidence
  -> Trace/State/Reporter/UI/Eval
```

## Mode Model

The default solve, resume, and eval brain is graph.

- graph: production mode. LangGraph plus PydanticAI is required. Missing or invalid provider configuration fails clearly and never falls back to deterministic solving.
- fallback: explicit offline deterministic compatibility mode. This is the only mode that uses the old deterministic planner/executor/verifier route.
- llm and hybrid: deprecated legacy compatibility modes. The CLI accepts them during the compatibility period and prints a deprecation warning. They are never the default.

The existing single, specialist, and critic-after-failures labels remain orchestration options inside fallback/legacy behavior, not the default brain selection.

## Specialist Role

Specialists should expose structured context, for example:

- recommended tools and why they apply
- artifact summaries and file-type signals
- likely next observations to collect
- safe local reproduction scaffolds
- category-specific prompt snippets

Specialists should not be the main solver. A Web specialist can say "this looks like PHP source disclosure plus LFI; collect source, summarize parameters, ask GPT/Codex to reason over it." It should not become a large PHP exploitation rule engine.

## Static Analyzer Role

Static analyzers are welcome when they make observations legible:

- recover code from rendered/highlighted output
- identify parameters, sinks, comparisons, constants, encodings, imports, file formats
- summarize constraints and candidate primitives
- emit JSON evidence for the LLM prompt and trace

They should stop at evidence and local-safe replay. Route selection remains the model's job.

## Success Criteria

- A configured LangGraph/PydanticAI provider is used as the default solve brain.
- Every model action is traceable to prompt context and prior observations.
- The same run can be resumed without losing reasoning context.
- Local benchmarks validate workflow reliability without requiring real network access.
- Memory improves context quality without polluting future runs with failed guesses.
- UI makes model decisions, commands, observations, artifacts, and verification state easy to inspect.

## Near-Term Refactor Direction

1. Keep graph as the default production brain while legacy llm/hybrid modes are phased out.
2. Keep deterministic specialist command generation behind explicit fallback-only paths.
3. Add a first-class context-provider phase before the graph reasoning step.
4. Treat static analyzers as observation summarizers that feed prompt JSON.
5. Make provider setup and doctor checks prominent, since PydanticAI provider availability is core product functionality.
6. Tighten memory quality gates so failed routes are low-confidence and never dominate prompt context.
