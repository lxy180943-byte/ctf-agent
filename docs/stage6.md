# Stage 6 Notes

阶段 6 实现无需真实 LLM 的可运行 MVP 解题循环。

## Agent 抽象

新增 `ctf_agent.agents.base`：

- `Agent`
  - `name`
  - `role`
  - `run(context)`
- `AgentContext`
  - `state`
  - `layout`
  - `trace_store`
  - `executor`
  - `tool_registry`
  - `config`
  - `max_steps`
  - `timeout`
  - `metadata`

## PlannerAgent

新增 `ctf_agent.agents.planner`：

- `Plan`
- `PlanCommand`
- `PlannerAgent`

当前 Planner 是确定性 MVP：

- 读取 Challenge 的描述、文件列表和 category。
- 查询 `ToolRegistry.recommend(category)` 记录推荐工具。
- 为每个题目文件生成 `python3` 文本扫描命令。
- 额外把 description 放进观察流，供 Verifier 扫描。
- 将 plan 写入 trace。

核心 triage 使用 `python3`，避免依赖完整 CTF 工具链；默认 Docker profile 已升级为 `ctf-agent:generic`，toy challenge 可在本地 executor 或 generic sandbox 中运行。

## ExecutorAgent

新增 `ctf_agent.agents.executor`：

- `ExecutionBatch`
- `ExecutorAgent`

ExecutorAgent 按 Plan 顺序执行命令：

- 使用阶段 4 的 `LocalExecutor` 或 `DockerExecutor`。
- 记录 `Step` 到当前 `Attempt`。
- 完整 stdout/stderr 由 executor 写入 artifact。
- trace 中保留 stdout/stderr 摘要。

## VerifierAgent

新增 `ctf_agent.agents.verifier`：

- `VerificationResult`
- `VerifierAgent`

Verifier 从以下来源提取 flag：

- command stdout
- command stderr
- stdout/stderr artifact

优先使用 challenge 的 `flag_regex`，并追加默认 pattern：

- `flag{...}`
- `FLAG{...}`
- `ctf{...}`
- `CTF{...}`

当前验证策略是 MVP：正则命中即标记 `verified=True`，但 `submitted=False`，不进行任何真实提交。

## Orchestrator

新增 `ctf_agent.core.orchestrator`：

- `Orchestrator`
- `SolveResult`

Orchestrator 负责：

- 创建或恢复 workspace run。
- 管理 `new -> analyzing -> running -> verifying -> solved/failed` 状态转换。
- 管理 `max_steps` 和 per-command `timeout`。
- 创建 trace store。
- 选择 executor。
- 保存 `state.json`。
- 从 `state.json` / `trace.jsonl` resume。

Docker 不可用时保持阶段 4 行为：

- 默认 Docker executor 自动降级到 LocalExecutor。
- 显式指定 Docker 时返回清晰错误。

## CLI

新增命令：

```bash
ctf-agent solve examples/challenge1 --max-steps 10
ctf-agent resume ~/ctf-workspace/runs/challenge1
```

支持参数：

```bash
--max-steps 10
--timeout 60
--executor local
--executor docker
--json
```

## Toy Challenge

`examples/challenge1` 现在同时作为 toy challenge：

- `challenge.yaml`
- `prompt.txt`
- flag：`flag{example_only}`

实际验证：

```text
ctf-agent solve examples/challenge1 --max-steps 10
challenge: challenge1
state: solved
run_dir: /home/liuxinyue/ctf-workspace/runs/challenge1
steps_executed: 2
flag: flag{example_only}

ctf-agent resume ~/ctf-workspace/runs/challenge1
challenge: challenge1
state: solved
run_dir: /home/liuxinyue/ctf-workspace/runs/challenge1
steps_executed: 0
flag: flag{example_only}
```

生成文件：

```text
~/ctf-workspace/runs/challenge1/state.json
~/ctf-workspace/runs/challenge1/trace.jsonl
~/ctf-workspace/runs/challenge1/work/prompt.txt
~/ctf-workspace/runs/challenge1/artifacts/command-output/*.stdout.txt
~/ctf-workspace/runs/challenge1/artifacts/command-output/*.stderr.txt
```

## 测试

新增测试覆盖：

- Planner 生成 Python scan plan。
- Verifier 从 command result 和 artifact 提取 flag。
- Orchestrator solve toy challenge。
- Orchestrator resume solved run。
- state 文件包含 solved 状态、attempt 结束时间和 flag candidate。
- CLI `solve`。
- CLI `resume`。

验证结果：

```text
pytest
65 passed in 3.13s
```

安全边界保持不变：不真实提交 flag，默认仅处理授权 CTF、本地靶场、比赛平台和 benchmark。
