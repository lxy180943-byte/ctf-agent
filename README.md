# CTF Agent

本项目是一个本地专用 CTF 解题 agent 骨架，目标运行在 WSL Linux 文件系统中。它只用于授权 CTF、本地靶场、比赛平台和 benchmark，不用于真实未授权目标。

## 路径约定

- 项目目录：`~/ctf-agent`
- 题目工作区：`~/ctf-workspace`
- Windows GUI 工具交换目录：`~/ctf-artifacts`

高频读写目录保持在 WSL Linux 文件系统中，Windows 只通过 artifacts 目录交换需要 IDA、010 Editor、Burp、Wireshark、浏览器等 GUI 工具处理的文件。

## 快速检查

```bash
cd ~/ctf-agent
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
ctf-agent --help
ctf-agent version
ctf-agent doctor
```

开发测试：

```bash
python -m pip install -e ".[dev]"
pytest
```

工程检查：

```bash
make doctor
make lint-basic
make test
make eval-local
make clean-generated
```

Docker sandbox profiles:

```bash
make docker-build-generic
make docker-build
make docker-doctor
ctf-agent docker doctor --run-tools
```

WSL 首次初始化：

```bash
scripts/bootstrap_wsl.sh
```

Ubuntu 24.04 默认启用 PEP 668，因此项目开发安装放在 `.venv` 中，不写入系统 Python。


## Solve brain modes

默认 solve/resume/eval brain 是 graph。Graph 模式使用 LangGraph + PydanticAI；provider 缺失或无效时会明确失败，不会自动进入确定性 fallback。

Graph 模式环境变量示例：

    export CTF_AGENT_LLM_PROVIDER=openai
    export OPENAI_API_KEY=replace-with-your-openai-api-key
    export OPENAI_MODEL=replace-with-model-name
    export OPENAI_BASE_URL=replace-with-openai-compatible-base-url
    ctf-agent doctor llm
    ctf-agent solve examples/challenge1 --executor local

离线或只验证本地确定性 plumbing 时显式使用 fallback：

    ctf-agent solve examples/challenge1 --brain fallback --executor local --max-steps 10
    ctf-agent eval ./evals/datasets/local --brain fallback --executor local --max-steps 20

--brain llm 和 --brain hybrid 仅为兼容期 legacy 模式，CLI 会输出 deprecation 提示。

## 安全边界

- 默认不自动攻击公网目标，除非 challenge 明确给出比赛靶机地址。
- 默认不真实提交 flag；提交相关能力必须 dry-run，除非显式传入 `--submit`。
- Docker 是题目运行和工具隔离的主 sandbox。
- Docker sandbox 按题型使用 `ctf-agent:generic`、`ctf-agent:pwn`、`ctf-agent:web`、`ctf-agent:crypto`、`ctf-agent:rev`、`ctf-agent:forensics`，避免一个巨大镜像。
- GPL/AGPL 参考项目只借鉴设计，不复制源码、提示词、配置或文档。

## 当前阶段

阶段 1 已建立 Python 项目骨架：

- `pyproject.toml`
- `configs/default.yaml`
- `src/ctf_agent/`
- `ctf-agent --help`
- `ctf-agent version`
- `ctf-agent doctor`
- 默认配置加载和环境变量覆盖
- console 日志与 JSONL trace 写入预留
- pytest 测试通过

阶段 2 已建立 core 运行基础：

- Challenge、Attempt、Step、Observation、Artifact、FlagCandidate 数据模型
- ChallengeState 状态机
- 每题独立 workspace：`~/ctf-workspace/runs/<challenge_id>/`
- `state.json` 保存与恢复
- `trace.jsonl` 逐步追加和 resume 读取

阶段 3 已建立 platforms 层：

- `PlatformAdapter` 抽象接口
- `LocalPlatformAdapter` 从本地 `challenge.yaml` 或目录导入
- `CTFdPlatformAdapter` 骨架，真实提交默认 dry-run
- CLI 支持 `ctf-agent list examples/` 和 `ctf-agent inspect examples/challenge1`

阶段 4 已建立执行器与 sandbox：

- `Executor` 抽象接口
- `LocalExecutor` 限制 cwd 在 workspace 内
- `DockerExecutor` mount workspace 到 `/workspace`
- 按题型配置 Docker 镜像 profile
- 命令完整 stdout/stderr 写 artifact，trace 只保留摘要
- CLI 支持 `ctf-agent exec <challenge_dir> -- "command"` 和 `ctf-agent doctor executors`

阶段 5 已建立 tools 层：

- `ToolSpec` 和 `ToolRegistry`
- 内置 generic、pwn、rev、crypto、web、forensics 工具清单
- `ctf-agent tools list`
- `ctf-agent tools doctor`
- 缺工具不崩溃，并输出安装建议

阶段 6 已建立无 LLM MVP 解题循环：

- `Agent` 抽象和 `AgentContext`
- `PlannerAgent` 生成确定性 plan
- `ExecutorAgent` 执行 plan
- `VerifierAgent` 提取 flag 候选
- `Orchestrator` 管理状态、step limit、timeout、trace、resume
- CLI 支持 `ctf-agent solve examples/challenge1 --max-steps 10`
- CLI 支持 `ctf-agent resume ~/ctf-workspace/runs/challenge1`

阶段 7 已加入可配置 LLM 决策层：

- `LLMProvider` 抽象接口
- OpenAI-compatible provider，`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL` 只从环境变量读取
- `DummyProvider` 用于测试
- `prompts/` 下提供 planner、executor、verifier、reporter 和 specialist prompts
- LLM 只输出结构化 JSON action，命令仍必须经过 Executor
- 默认 graph 模式要求 PydanticAI provider；LLM 不可用时必须显式传入 --brain fallback 才使用无 LLM 确定性流程

阶段 8 已加入多专家协作：

- `CategoryClassifier` 按描述、连接、附件、magic 判断题型
- `SpecialistAgent` 基类和 pwn/web/crypto/rev/forensics 专家
- `CriticAgent` 在连续失败达到阈值后提出替代策略
- `AgentMessageBus` 共享假设、观察、flag 候选和失败原因
- Orchestrator 支持 `single`、`specialist`、`critic-after-failures`

阶段 9 已完善验证和产物输出：

- `FlagDetector` 支持 challenge `flag_regex`、常见 flag 格式和配置中的自定义格式
- `VerifierAgent` 从 stdout、stderr、artifact 和 workspace 文件中提取候选并去重排序
- `Submitter` 支持 local 标记 solved 与 CTFd 提交骨架，默认始终 dry-run，只有 `--submit` 才真实提交
- `Reporter` 生成 `writeup.md`，记录题目信息、关键命令、失败路线、最终 flag 和复现步骤
- CLI 支持 `ctf-agent report <run_dir>` 和 `ctf-agent submit <run_dir> --dry-run`

阶段 9 常用命令：

```bash
ctf-agent solve examples/challenge1 --brain fallback --executor local --max-steps 10
ctf-agent report ~/ctf-workspace/runs/challenge1
ctf-agent submit ~/ctf-workspace/runs/challenge1 --dry-run
```

阶段 10 已加入 memory：

- `KnowledgeItem` 记录 category、pattern、symptom、solution、commands、source_run、confidence
- `MemoryStore` 使用 SQLite，默认 `~/ctf-workspace/memory/knowledge.sqlite`
- CLI 支持 `ctf-agent memory search/add/learn`
- Planner 在新题开始前检索相关经验，并写入 trace/plan metadata
- Orchestrator 从 solved run 自动学习有效路线，从 failed run 或无效命令生成失败复盘
- 所有知识项必须带 `source_run`，可追溯到原始 run

阶段 10 常用命令：

```bash
ctf-agent memory learn ~/ctf-workspace/runs/challenge1
ctf-agent memory search "text flag" --category misc
```

阶段 11 已加入 evals：

- `BenchmarkAdapter` 抽象接口
- `LocalBenchmark` 读取本地 benchmark 数据集
- 预留 Cybench、NYU CTF Bench、Cyber-Zero 适配器
- 指标包括 solved_count、steps_used、time_used、command_count、verifier_false_positive、resume_success
- CLI 支持 `ctf-agent eval ./evals/datasets/local --max-steps 20`
- 输出 `eval_report.md`、`eval_results.jsonl` 和 `eval_summary.json`

阶段 11 常用命令：

```bash
ctf-agent eval ./evals/datasets/local --brain fallback --executor local --max-steps 20
```

阶段 12 已加入轻量 Web UI：

- 标准库本地 Web 服务，无新增运行依赖
- 工作台第一屏包含 challenge 列表、run 状态、trace、文件/artifact、flag candidates、writeup 和提交控制
- 默认绑定 `127.0.0.1:8008`
- 手动提交默认 dry-run，真实提交必须输入 `SUBMIT`

阶段 12 常用命令：

```bash
ctf-agent ui --challenges examples
```
