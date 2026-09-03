# Evaluation Framework

The eval harness supports local benchmark datasets and conversion skeletons for future Cybench, NYU CTF Bench, and Cyber-Zero imports.

## Challenge Metadata

Each local challenge may define benchmark fields under `metadata`:

```yaml
metadata:
  expected_flags:
    - flag{demo}
  max_time: 60
  difficulty: easy
  tags:
    - rsa
    - warmup
  required_tools:
    - python3
```

`expected_flag` remains supported as a shorthand for one expected flag.

## Running

```bash
ctf-agent eval ./evals/datasets/local --executor local --mode specialist --max-steps 30
ctf-agent eval ./evals/datasets/local --only-category crypto --only-tag rsa
ctf-agent eval ./evals/datasets/local --repeat 3 --regression
ctf-agent eval ./evals/datasets/local --fail-fast
```

Each result row includes a scorecard with solved/false-positive status, tools used, stuck stage, max-time status, trace summary, and next suggestions.

## Regression Mode

`--repeat N` runs the same filtered dataset N times. Regression output compares each repeat against repeat 1 for solved count, steps, and time.

## External Dataset Skeletons

`CybenchAdapter`, `NYUCTFBenchAdapter`, and `CyberZeroAdapter` currently provide a conversion skeleton. The shared fallback expects a `manifest.jsonl` with fields like `id`, `title`, `category`, `description`, `files`, `expected_flags`, `difficulty`, `tags`, and `required_tools`, then writes a local benchmark layout. Dataset-specific parsers can replace `export_records()` later.

## Memory

Eval results are written to memory when `memory.enabled=true`, but every item is tagged:

```json
{"kind": "eval-benchmark-result", "experience_scope": "benchmark"}
```

This keeps benchmark experience separate from real competition experience.


## Expected Flag Isolation

Reasoning benchmarks may store `expected_flag` or `expected_flags` for evaluator scoring only. These values, answer files, solution fields, ground-truth fields, and any `evaluator_only` artifacts are private evaluator metadata.

Before a benchmark case reaches `Orchestrator`, the runner uses `sanitize_benchmark_for_solver()` to construct a solver-visible `Challenge` with evaluator-only fields removed. The solver-visible challenge, workspace input files, EvidencePacket, SolverDependencies, trace, memory, and reports must not contain the expected flag or answer artifact path.

The evaluator can still compare verified solver output against the private expected flags and records only `expected_flag_matched`, `solve_success`, and `verifier_false_positive` by default. Do not print expected flag values in reasoning benchmark reports unless a future explicit evaluator-only debug switch is added.
