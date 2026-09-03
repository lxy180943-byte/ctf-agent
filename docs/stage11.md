# Stage 11: Evals

更新日期：2026-08-31

## Scope

阶段 11 引入 benchmark harness，用本地 toy 数据集评估 solve loop，并为后续 Cybench、NYU CTF Bench、Cyber-Zero 预留适配器。

## Implemented

- `BenchmarkAdapter`
- `BenchmarkChallenge`
- `LocalBenchmark`
- `BenchmarkRunner`
- `EvalChallengeResult`
- `EvalSummary`
- 预留适配器：
  - `CybenchAdapter`
  - `NYUCTFBenchAdapter`
  - `CyberZeroAdapter`

## Metrics

- `solved_count`
- `steps_used`
- `time_used`
- `command_count`
- `verifier_false_positive`
- `resume_success`

若数据集 challenge metadata 提供 `expected_flag` 或 `expected_flags`，runner 会用它检测 verifier false positive。

## CLI

```bash
ctf-agent eval ./evals/datasets/local --max-steps 20
```

可选参数：

```bash
ctf-agent eval ./evals/datasets/local --executor local --output-dir ~/ctf-workspace/evals/local-smoke
```

输出文件：

- `eval_report.md`
- `eval_results.jsonl`
- `eval_summary.json`

## Local Dataset

新增本地 toy benchmark：

- `evals/datasets/local/crypto-basic`
- `evals/datasets/local/forensics-basic`
- `evals/datasets/local/web-basic`

这些 toy challenge 都是本地授权样例，flag 存在于附件中，便于稳定回归测试。

## Tests

新增 `tests/test_evals.py`，覆盖：

- `LocalBenchmark` 读取本地数据集和 expected flags
- `BenchmarkRunner` 写入 `eval_report.md` 和 `eval_results.jsonl`
- false positive 指标
- CLI `ctf-agent eval`
- Cybench、NYU CTF Bench、Cyber-Zero placeholder 行为

最终验证结果：

```text
. .venv/bin/activate && python -m pytest tests/test_evals.py -q
5 passed

. .venv/bin/activate && make test
108 passed in 4.91s

. .venv/bin/activate && make doctor
CTF Agent Environment Doctor
OK: True
Docker smoke attempted=True ok=True

CTF_AGENT_WORKSPACE_DIR=~/ctf-workspace-stage11 CTF_AGENT_MEMORY_ENABLED=false ctf-agent eval ./evals/datasets/local --executor local --max-steps 20 --output-dir ~/ctf-workspace-stage11/evals/local-smoke
dataset: local
output_dir: /home/liuxinyue/ctf-workspace-stage11/evals/local-smoke
solved_count: 3/3
steps_used: 6
time_used: 0.091197
command_count: 6
verifier_false_positive: 0
resume_success: 3
eval_report: /home/liuxinyue/ctf-workspace-stage11/evals/local-smoke/eval_report.md
eval_results: /home/liuxinyue/ctf-workspace-stage11/evals/local-smoke/eval_results.jsonl
```

## Safety Notes

- eval 只通过现有 Orchestrator 运行 challenge。
- 命令仍必须经过 Executor 和 workspace 边界检查。
- 默认仍不会真实提交 flag。
- 外部 benchmark 适配器仅预留接口，不联网、不下载数据。
- GPL/AGPL 参考项目只借鉴 benchmark harness 思想，没有复制源码、配置、提示词、文档或测试数据。
