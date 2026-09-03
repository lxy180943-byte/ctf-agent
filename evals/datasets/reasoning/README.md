# Reasoning Benchmarks

Reasoning benchmark cases may include evaluator-only expected flags and answer artifacts in dataset metadata. Solver-visible `Challenge` objects must be produced through `sanitize_benchmark_for_solver`, which removes expected flags, solution fields, evaluator-only metadata, hints, and evaluator-only artifact paths before orchestration.
