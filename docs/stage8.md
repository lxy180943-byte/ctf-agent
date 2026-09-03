# Stage 8 Notes

阶段 8 实现多专家协作：题型分类、专家 agent、critic 和共享消息总线。

## CategoryClassifier

新增 `ctf_agent.agents.classifier`：

- `CategoryClassifier`
- `CategoryClassification`

分类依据：

- challenge metadata category
- title / description / hints / connection 关键词
- connection 类型，例如 HTTP URL 偏向 web
- 附件扩展名
- 附件 magic bytes

当前支持 category：

- `pwn`
- `web`
- `crypto`
- `rev`
- `forensics`
- `misc`

分类结果会写入 trace，并进入 `state.metadata.classification`。

## SpecialistAgent

新增 `ctf_agent.agents.specialists`：

- `SpecialistAgent`
- `PwnAgent`
- `WebAgent`
- `CryptoAgent`
- `RevAgent`
- `ForensicsAgent`
- `specialist_for_category(category)`

专家约束：

- specialist 只生成 Plan，不执行命令。
- 命令仍由 `ExecutorAgent` 和阶段 4 的 executor 安全策略执行。
- specialist 必须通过 `ToolRegistry.recommend(category)` 选择工具。
- 每个专家 plan metadata 记录 `selected_tools`。
- 所有专家保留 Python 文本扫描 fallback，避免缺工具时直接失效。

## CriticAgent

新增 `ctf_agent.agents.critic.CriticAgent`。

`critic-after-failures` 模式下：

- 初始路线失败后记录 failure reason。
- 当连续失败次数达到 `critic_after_failures`，CriticAgent 提出替代策略。
- 当前替代策略是非破坏性的 workspace flag pattern 扫描。
- Critic 只提出 Plan，命令仍走 Executor。

## AgentMessageBus

新增 `ctf_agent.agents.message_bus`：

- `AgentMessageBus`
- `AgentMessage`

共享内容类型：

- `hypothesis`
- `observation`
- `flag_candidate`
- `failure_reason`

Orchestrator 会将 message bus 快照放入 `SolveResult.metadata.message_bus`。

## Orchestrator 模式

`Orchestrator` 新增模式：

- `single`
  - 维持阶段 6/7 的 PlannerAgent 路线。
- `specialist`
  - CategoryClassifier 分类后路由到对应 SpecialistAgent。
- `critic-after-failures`
  - 先走 specialist 路线。
  - 达到失败阈值后调用 CriticAgent。

配置：

```yaml
orchestration:
  mode: single
  critic_after_failures: 2
```

环境变量：

```bash
CTF_AGENT_ORCHESTRATION_MODE=specialist
CTF_AGENT_CRITIC_AFTER_FAILURES=1
```

CLI：

```bash
ctf-agent solve examples/challenge1 --mode single
ctf-agent solve examples/challenge1 --mode specialist
ctf-agent solve examples/challenge1 --mode critic-after-failures --critic-after-failures 1
```

## 验证

Specialist 模式：

```text
ctf-agent solve examples/challenge1 --executor local --mode specialist --max-steps 10
challenge: challenge1
state: solved
run_dir: /home/liuxinyue/ctf-workspace/runs/challenge1
steps_executed: 3
flag: flag{example_only}
```

Critic-after-failures 模式：

```text
ctf-agent solve examples/challenge1 --executor local --mode critic-after-failures --critic-after-failures 1 --max-steps 10
challenge: challenge1
state: solved
run_dir: /home/liuxinyue/ctf-workspace/runs/challenge1
steps_executed: 3
flag: flag{example_only}
```

测试覆盖：

- 分类器按描述、连接、文件 magic 分类。
- 消息总线收集假设、观察、失败原因、flag 候选。
- specialist 通过 ToolRegistry 选工具。
- specialist mode 可解 toy challenge。
- critic-after-failures 可在初始路线失败后用替代策略恢复。
- CLI `--mode specialist`。

验证结果：

```text
pytest
86 passed in 3.37s
```

安全边界保持不变：专家和 critic 只生成计划，不执行命令、不提交 flag、不针对未授权目标。
