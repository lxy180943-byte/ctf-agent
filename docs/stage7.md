# Stage 7 Notes

阶段 7 加入可配置 LLM 决策层，同时保持无 LLM fallback。

## LLM Provider

新增 `ctf_agent.llm.provider`：

- `LLMProvider`
- `LLMMessage`
- `LLMResponse`
- `OpenAICompatibleProvider`
- `DummyProvider`
- `build_provider(config, environ=None)`

OpenAI-compatible provider 使用标准 `/chat/completions` 接口。`base_url`、`api_key`、`model` 不写入 YAML，只从 OpenAI 环境变量读取。

默认不启用 LLM：

```yaml
llm:
  provider: none
```

环境变量配置：

```bash
CTF_AGENT_LLM_PROVIDER=openai-compatible
OPENAI_BASE_URL=https://example.invalid/v1
OPENAI_API_KEY=replace-with-your-key
OPENAI_MODEL=replace-with-model-name
```

`DummyProvider` 用于测试和离线调试，可以预置 JSON 响应并记录 calls。

## Prompts

新增 `prompts/`：

- `planner.md`
- `executor.md`
- `verifier.md`
- `reporter.md`
- `specialist_pwn.md`
- `specialist_web.md`
- `specialist_crypto.md`
- `specialist_rev.md`
- `specialist_forensics.md`

所有 prompt 都强调：

- 只输出结构化 JSON。
- 不伪造 stdout、stderr、文件、flag 或工具结果。
- 每次只给少量命令或 action。
- 不提交 flag。
- 命令只是建议，必须交给 Executor 执行。
- 网络工具只用于 challenge 明确提供的授权目标。

## Prompt Rendering

新增 `ctf_agent.llm.prompts`：

- `PromptStore`
- `render_template`

模板变量格式：

```text
{{challenge_json}}
{{tools_json}}
{{observation_json}}
```

## JSON Action Parsing

新增 `ctf_agent.llm.actions`：

- `parse_json_object(text)`
- `extract_command_actions(data, max_actions=3)`

解析器只接受 JSON object；命令 action 最多取 3 条，并过滤空 command。

## Agent 集成

`PlannerAgent`：

- 有 provider 时尝试调用 LLM。
- LLM 必须返回 JSON plan。
- 解析成功后生成 `PlanCommand`。
- LLM 不存在、调用失败、JSON 无效或返回空命令时，回退到 deterministic plan。

`VerifierAgent`：

- 先使用正则提取候选。
- 正则无候选且存在 provider 时，可调用 LLM 标注候选。
- LLM 候选必须逐字出现在 observations 中，否则拒绝，防止伪造 flag。
- 仍然不提交 flag，候选默认 `submitted=False`。

`Orchestrator`：

- 从 config/env 构建 provider。
- `llm.provider: none` 时使用纯 deterministic loop。
- OpenAI 连接信息只从 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL` 读取。
- provider 配置失败时记录 fallback，不阻断 solve。
- LLM 只负责决策，命令执行仍经过 `LocalExecutor` / `DockerExecutor` 和安全策略。

## 验证

默认无 LLM 路径仍可自动解 toy challenge：

```text
ctf-agent solve examples/challenge1 --max-steps 10
challenge: challenge1
state: solved
run_dir: /home/liuxinyue/ctf-workspace/runs/challenge1
steps_executed: 2
flag: flag{example_only}
```

Dummy provider 环境变量路径可运行，并在空 plan 时 fallback：

```text
CTF_AGENT_LLM_PROVIDER=dummy ctf-agent solve examples/challenge1 --executor local --max-steps 10 --json
"solved": true
"flag{example_only}"
```

测试覆盖：

- prompt 渲染。
- prompt 文件加载。
- DummyProvider mock。
- OpenAI-compatible provider HTTP mock。
- JSON action 解析。
- Planner 使用 DummyProvider。
- Verifier 拒绝未出现在 observation 中的伪造候选。
- Orchestrator 使用 DummyProvider，但命令仍经过 Executor。
- LLM 环境变量覆盖。

验证结果：

```text
pytest
77 passed in 2.76s
```

安全边界保持不变：LLM 不执行命令，不提交 flag，不伪造结果，不扩展到未授权目标。
