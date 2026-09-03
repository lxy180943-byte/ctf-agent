# CTF Agent Implementation Plan

更新日期：2026-08-31

## 阶段 0 环境基线

目标是在 WSL Linux 文件系统中从零搭建本地专用 CTF 解题 agent：

- 项目目录：`/home/liuxinyue/ctf-agent`
- 题目运行工作区：`/home/liuxinyue/ctf-workspace`
- Windows GUI 工具交换目录：`/home/liuxinyue/ctf-artifacts`

安全边界：

- 仅用于授权 CTF、本地靶场、比赛平台和 benchmark。
- 不针对真实未授权目标。
- 默认不自动攻击公网目标，除非 challenge 明确给出比赛靶机地址。
- 自动提交 flag 默认 dry-run，必须显式传入 `--submit` 才真实提交。

## 环境检查结果

当前执行环境确认是 WSL2 Linux：

- Kernel：`Linux 6.6.87.2-microsoft-standard-WSL2 x86_64`
- 发行版：`Ubuntu 24.04`
- WSL 发行版名：`Ubuntu-24.04`
- Home：`/home/liuxinyue`
- 初始 Windows 调用目录：`/mnt/d/ctf-tools`
- 高频项目、题目工作区、导出目录均已创建在 Linux home 下，避免放入 `/mnt/c` 或 `/mnt/d`。

工具链检查：

| 工具 | 状态 | 版本 / 备注 |
| --- | --- | --- |
| `python3` | 可用 | `Python 3.12.3` |
| `python` | 缺失 | 后续可安装 `python-is-python3` 或统一只调用 `python3` |
| `pip` | 可用 | `pip 25.2` |
| `pip3` | 可用 | `pip 25.2` |
| `uv` | 缺失 | 后续可作为 Python 依赖管理器安装 |
| `git` | 可用 | `git version 2.43.0` |
| `docker` | 可用 | `Docker version 29.6.2` |
| `docker compose` | 可用 | `Docker Compose version v5.3.1` |
| `rg` | 缺失 | 后续建议安装 Linux 版 ripgrep |
| `gh` | 缺失 | GitHub 集成阶段再安装 |
| `make` | 可用 | `GNU Make 4.3` |

Docker 检查：

- `docker info` 可连接 Docker daemon。
- `docker run --rm hello-world` 成功。
- Docker 将作为题目运行、工具链隔离和危险命令约束的主 sandbox。

阶段 0 新增的可重复检查入口：

- `scripts/doctor.py`
- `make doctor`
- `make test`

阶段 0 运行结果：

```text
python3 -m unittest discover -s tests -v
Ran 3 tests in 0.001s
OK

python3 scripts/doctor.py --create-dirs --docker-run
CTF Agent Environment Doctor
OK: True
```

## 项目目录规划

```text
ctf-agent/
  core/          # 任务状态、事件模型、运行上下文、安全策略
  platforms/     # CTFd、本地 benchmark、手工题目目录等 platform adapter
  agents/        # Planner、Executor、Verifier、Critic、specialist agents
  sandbox/       # Docker sandbox、资源限制、网络策略、文件挂载策略
  tools/         # 文件分析、二进制、Web、Crypto、Forensics、Pwn 工具封装
  memory/        # trace、resume、知识库、失败复盘、案例索引
  evals/         # benchmark runner、评分、预算统计、回归任务
  cli/           # 命令行入口
  configs/       # 默认配置、模型配置、安全策略配置
  docs/          # 设计文档、操作手册、阶段记录
  examples/      # 示例 challenge、示例配置、dry-run 示例
  tests/         # 单元测试、集成测试、sandbox smoke tests
  scripts/       # doctor、开发辅助脚本
```

## MVP 范围

第一版最小可用目标：

- 提供 CLI：`doctor`、`init-challenge`、`solve`、`resume`、`export-artifacts`。
- 支持本地 challenge 目录作为输入，生成标准化 task manifest。
- 内置 Planner / Executor / Verifier 三段式循环，先实现规则和工具驱动流程，再接模型适配层。
- 默认所有外部提交为 dry-run；真实提交必须显式 `--submit`。
- Docker sandbox 支持只读题目挂载、可写工作目录、CPU/内存/超时限制。
- 工具先覆盖通用 triage：`file`、`strings`、hash、解压、文本搜索、HTTP 基础探测、Python 脚本执行。
- 记录 JSONL trace，支持失败后 resume。
- flag 检测先实现可配置正则和人工确认入口。
- 输出 artifacts 到 `/home/liuxinyue/ctf-artifacts`，供 Windows GUI 工具打开。

## 后续增强

- Platform adapters：CTFd、rCTF、picoCTF 风格 benchmark、本地目录 benchmark。
- Specialist agents：web、pwn、rev、crypto、forensics、misc。
- Critic / verifier：对 exploit、flag、提交动作做独立复核。
- Auto-prompter：根据失败 trace 生成下一轮更具体的任务提示。
- Budget control：token、时间、Docker 执行次数、网络访问次数预算。
- Shared messages：多 solver 并行时共享发现、候选 flag、失败原因。
- UI/workstation：文件浏览、trace 查看、artifact 导出、Windows GUI handoff。
- 模型适配：OpenAI-compatible、本地模型、可插拔 provider registry。
- 知识库：题型 playbook、历史 writeup 摘要、本地可检索失败复盘。

## 许可证与参考项目风险

参考项目只借鉴架构思想和交互模式，不直接复制源码、提示词、配置文件、文档文本或测试数据。

- `0ca/BoxPwnr` 标注为 AGPL：只借鉴 platform adapter、benchmark、trace、resume、预算控制等设计概念。
- `NUSGreyhats/ctf-agent-workstation` 标注为 GPL：只借鉴 UI、flag 检测、文件浏览、GDB/IDA/WireGuard 等产品功能思路。
- 对 GPL/AGPL 项目不做代码级搬运，不做派生文件，不复制函数结构。
- 若未来需要引入任何第三方代码，必须先记录许可证、版本、来源 URL、兼容性判断，并在依赖清单中固定。
- 当前阶段没有引入第三方项目源码。

## 测试策略

测试分层：

- Unit tests：核心状态机、manifest 解析、安全策略、flag detector、tool registry。
- Integration tests：CLI 命令、Docker sandbox、artifact export、resume trace。
- Golden tests：典型 challenge 输入对应稳定的 trace/event 输出。
- Safety tests：默认 dry-run、未授权公网目标拒绝、提交动作必须显式 `--submit`。
- Smoke tests：`make doctor`、Docker hello-world、sandbox 最小命令。
- Benchmark tests：后续用公开 CTF benchmark 和本地 toy challenges 评估 solve loop。

阶段 0 已实现：

- `tests/test_doctor.py` 覆盖 Docker Compose 参数、目录创建、WSL 环境标记识别。
- `make test` 通过。
- `make doctor` 通过。

## 阶段 1 建议

下一阶段建议建立真正的 Python 包和 CLI：

- 选择依赖管理方式：若安装 `uv`，使用 `pyproject.toml` + `uv.lock`；否则先用标准库和 `pip` 保持最小依赖。
- 建立 `ctf_agent` 包目录、CLI 入口、配置加载、安全策略对象。
- 加入 challenge manifest schema 和本地 challenge 初始化命令。
- 把 doctor 从 `scripts/` 逐步迁移或包装到 `ctf-agent doctor`。

## 阶段 1 实施记录

更新日期：2026-08-31

阶段 1 已完成 Python 项目骨架：

- 新增 `pyproject.toml`，使用 `setuptools` 和 `src/` 布局。
- 新增 `ctf-agent` console script，入口为 `ctf_agent.cli.app:main`。
- 新增 `README.md`、`.gitignore`、`configs/default.yaml`。
- 在 `src/ctf_agent/` 下创建 `core`、`platforms`、`agents`、`sandbox`、`tools`、`memory`、`evals`、`cli`。
- 保留 `tests/`、`docs/`、`examples/`，并补充 `docs/stage1.md`。

阶段 1 CLI：

- `ctf-agent --help`
- `ctf-agent version`
- `ctf-agent doctor`
- `ctf-agent doctor --json`
- `ctf-agent doctor --skip-docker-run`

配置加载：

- 默认读取 `configs/default.yaml`。
- 支持 `--config` 指定配置文件。
- 支持 `CTF_AGENT_CONFIG` 指定配置文件。
- 支持常用环境变量覆盖：
  - `CTF_AGENT_WORKSPACE_DIR`
  - `CTF_AGENT_ARTIFACTS_DIR`
  - `CTF_AGENT_LOG_LEVEL`
  - `CTF_AGENT_TRACE_ENABLED`
  - `CTF_AGENT_TRACE_PATH`
  - `CTF_AGENT_SUBMIT_ENABLED`
  - `CTF_AGENT_DOCKER_IMAGE`
  - `CTF_AGENT_DOCKER_NETWORK`
- 支持通用嵌套覆盖格式：`CTF_AGENT_CONFIG__SECTION__KEY=value`。

日志与 trace：

- `ctf_agent.core.logging.setup_logging` 初始化普通 console 日志。
- `JsonlTraceWriter` 预留 JSONL trace 写入能力。
- 默认 trace 路径为 `/home/liuxinyue/ctf-workspace/traces/ctf-agent.jsonl`。

阶段 1 测试计划：

- 使用 `pytest` 运行测试。
- 测试覆盖 CLI、配置加载、环境变量覆盖、doctor、JSONL trace。
- Ubuntu 24.04 启用 PEP 668，因此开发依赖安装在项目 `.venv` 中。
- 若环境没有 `pytest`，通过 `. .venv/bin/activate && python -m pip install -e ".[dev]"` 安装开发依赖。

阶段 1 实际验证结果：

```text
. .venv/bin/activate && ctf-agent --help
usage: ctf-agent [-h] [--config CONFIG] {version,doctor} ...

. .venv/bin/activate && ctf-agent version
ctf-agent 0.1.0

. .venv/bin/activate && pytest
12 passed in 0.76s

. .venv/bin/activate && ctf-agent doctor
CTF Agent Environment Doctor
OK: True
Docker smoke attempted=True ok=True
```

许可证边界复核：

- 当前阶段未复制任何参考项目源码。
- AGPL/GPL 参考项目继续只借鉴设计思想，不直接复制代码、提示词、配置、文档或测试数据。

## 阶段 2 实施记录

更新日期：2026-08-31

阶段 2 已完成 core 数据模型和状态机：

- `ctf_agent.core.models`
  - `Challenge`
  - `Attempt`
  - `Step`
  - `Observation`
  - `Artifact`
  - `FlagCandidate`
- `ctf_agent.core.state`
  - `ChallengeState`
  - `ChallengeRunState`
  - `InvalidStateTransition`
- `ctf_agent.core.workspace`
  - `WorkspaceManager`
  - `WorkspaceLayout`
  - `ResumeData`
- `ctf_agent.core.trace`
  - `TraceEvent`
  - `TraceStore`
  - stdout/stderr 摘要截断

每题 workspace 默认布局：

```text
~/ctf-workspace/runs/<challenge_id>/
  input/
  work/
  artifacts/
  state.json
  trace.jsonl
```

状态机：

- `new`
- `analyzing`
- `running`
- `verifying`
- `solved`
- `failed`
- `paused`

关键约束：

- `solved` 是终态，不允许继续切回运行状态。
- `failed` 可回到 `analyzing`，支持人工修复后 resume。
- `paused` 可恢复到 `analyzing`、`running` 或 `verifying`。

Trace 记录字段：

- `challenge_id`
- `agent`
- `action`
- `command`
- `stdout`
- `stderr`
- `artifacts`
- `exit_code`
- `started_at`
- `ended_at`
- `timestamp`
- `metadata`

Resume：

- 从 `state.json` 恢复 `ChallengeRunState`。
- 从 `trace.jsonl` 恢复事件列表。
- `WorkspaceManager.resume(challenge_id)` 返回状态、trace 和路径布局。

阶段 2 实际验证结果：

```text
. .venv/bin/activate && pytest
23 passed in 0.72s
```

许可证边界复核：

- 当前阶段仍未复制任何参考项目源码。
- AGPL/GPL 项目继续只借设计，不直接复制代码、配置、提示词、文档或测试数据。

## Docker sandbox 多 profile 加固记录

更新日期：2026-08-31

已完成：

- 新增 `docker/` 目录。
- 新增 Dockerfiles：
  - `docker/Dockerfile.generic`
  - `docker/Dockerfile.pwn`
  - `docker/Dockerfile.web`
  - `docker/Dockerfile.crypto`
  - `docker/Dockerfile.rev`
  - `docker/Dockerfile.forensics`
- 新增 `ctf_agent.sandbox.images` 统一维护 profile 名称、本地镜像名、Dockerfile 路径、核心工具检查命令和可选工具说明。
- 更新 `configs/default.yaml`，把 category 映射到本地镜像：
  - generic -> `ctf-agent:generic`
  - pwn -> `ctf-agent:pwn`
  - web -> `ctf-agent:web`
  - crypto -> `ctf-agent:crypto`
  - rev -> `ctf-agent:rev`
  - forensics -> `ctf-agent:forensics`
  - misc -> `ctf-agent:generic`
  - sage -> `sagemath/sagemath:latest`
- 新增 CLI：
  - `ctf-agent docker build --profile all`
  - `ctf-agent docker build --profile generic`
  - `ctf-agent docker doctor`
  - `ctf-agent docker doctor --run-tools`
- 新增 Makefile targets：
  - `make docker-build`
  - `make docker-doctor`
  - `make docker-build-generic`
  - `make docker-build-pwn`
  - `make docker-build-web`
  - `make docker-build-crypto`
  - `make docker-build-rev`
  - `make docker-build-forensics`
- 新增 `.dockerignore`，避免 `.venv`、缓存、run artifacts 进入 Docker build context。
- 新增 `docs/docker_profiles.md`。

镜像拆分原则：

- 每个镜像只安装该题型常用工具。
- Sage 保持独立可选 profile，不并入 crypto。
- `one_gadget` 和 `zsteg` 提供占位/安装说明，避免默认镜像过度膨胀或构建不稳定。

安全边界：

- 默认 Docker runtime network 仍为 `none`。
- Docker executor 仍保留 workspace mount、timeout、memory、CPU 和危险命令检查。
- 只用于授权 CTF、本地靶场、比赛平台和 benchmark。

## 阶段 9 实施记录

更新日期：2026-08-31

阶段 9 已完成验证和产物输出：

- `ctf_agent.core.flag_detector`
  - `FlagDetector`
  - 支持 challenge `flag_regex`
  - 支持常见 `flag{...}`、`FLAG{...}`、`ctf{...}`、`CTF{...}`、`*CTF{...}` 格式
  - 支持 `verification.custom_flag_patterns` 自定义正则
  - 候选按置信度、值和来源去重排序
- `ctf_agent.agents.verifier`
  - 从命令 stdout/stderr 提取 flag
  - 从 stdout/stderr/text/report artifact 提取 flag
  - 从 workspace 中 challenge 声明的文件提取 flag
  - LLM verifier 只接受实际出现在 observation 中的候选
- `ctf_agent.core.submitter`
  - 默认 dry-run
  - local 平台 dry-run 只记录意图，`--submit` 才标记 solved/submitted
  - CTFd 平台沿用 `CTFdPlatformAdapter`，真实提交必须显式 `--submit`
  - 每次提交或 dry-run 写入 `state.metadata.last_submit`
- `ctf_agent.core.reporter`
  - 生成 `writeup.md`
  - 包含题目信息、描述、文件、关键命令、失败路线、最终 flag、复现步骤和原始 metadata

新增配置：

```yaml
verification:
  custom_flag_patterns: []
```

新增 CLI：

```bash
ctf-agent report ~/ctf-workspace/runs/challenge1
ctf-agent report ~/ctf-workspace/runs/challenge1 --json
ctf-agent submit ~/ctf-workspace/runs/challenge1 --dry-run
ctf-agent submit ~/ctf-workspace/runs/challenge1 --submit
ctf-agent submit ~/ctf-workspace/runs/challenge1 --flag 'flag{manual}'
```

安全边界复核：

- `ctf-agent submit` 未传 `--submit` 时不会真实提交。
- `--dry-run` 优先于 `--submit`，同时传入时仍按 dry-run 处理。
- CTFd URL 或 token 未配置时不会尝试真实提交。
- LLM 仍只做决策或候选标注，不执行命令、不伪造结果、不提交 flag。
- local submit 仅修改本地 run state，不触达外部平台。

阶段 9 测试覆盖：

- flag detector 使用 challenge regex、常见格式和自定义格式。
- detector 从文件和 artifact 中提取候选。
- verifier 在 stdout 为空时也能从 workspace 文件提取候选。
- local dry-run 不把候选标记为外部已提交。
- local `--submit` 可标记 run 为 solved，并设置候选 submitted。
- CTFd dry-run 不调用真实提交。
- reporter 生成包含命令和 flag 的 `writeup.md`。

阶段 9 实际验证结果：

```text
. .venv/bin/activate && make test
95 passed in 3.86s

. .venv/bin/activate && make doctor
CTF Agent Environment Doctor
OK: True
Docker smoke attempted=True ok=True

. .venv/bin/activate && ctf-agent solve examples/challenge1 --executor local --max-steps 10
challenge: challenge1
state: solved
run_dir: /home/liuxinyue/ctf-workspace/runs/challenge1
steps_executed: 2
flag: flag{example_only}

. .venv/bin/activate && ctf-agent report ~/ctf-workspace/runs/challenge1
writeup: /home/liuxinyue/ctf-workspace/runs/challenge1/writeup.md

. .venv/bin/activate && ctf-agent submit ~/ctf-workspace/runs/challenge1 --dry-run
dry_run: True
submitted: False
flag: flag{example_only}
```

许可证边界复核：

- 当前阶段没有引入第三方项目源码。
- AGPL/GPL 参考项目继续只借设计，不复制代码、配置、提示词、文档或测试数据。

## 阶段 10 实施记录

更新日期：2026-08-31

阶段 10 已完成 memory 层：

- `ctf_agent.memory.store`
  - `KnowledgeItem`
  - `MemoryStore`
  - SQLite schema 和轻量文本检索
  - solved/failed run 学习逻辑
- `ctf-agent memory search`
- `ctf-agent memory add`
- `ctf-agent memory learn`

KnowledgeItem 字段：

- `category`
- `pattern`
- `symptom`
- `solution`
- `commands`
- `source_run`
- `confidence`
- `metadata`
- `created_at`
- `id`

配置：

```yaml
memory:
  enabled: true
  path: ~/ctf-workspace/memory/knowledge.sqlite
  auto_learn: true
  search_limit: 5
```

环境变量：

```bash
CTF_AGENT_MEMORY_ENABLED=true
CTF_AGENT_MEMORY_PATH=~/ctf-workspace/memory/knowledge.sqlite
CTF_AGENT_MEMORY_AUTO_LEARN=true
CTF_AGENT_MEMORY_SEARCH_LIMIT=5
```

Planner 集成：

- planning 前按题目 title/category/description/hints/files 检索 memory。
- 匹配项写入 `TraceEvent(action="memory-search")`。
- 匹配项写入 plan metadata。
- LLM planner prompt 增加 `Relevant memory JSON`。
- Memory 不直接执行命令，仍由 Planner 生成 plan，再由 Executor 执行。

自动学习：

- solved run 生成 `metadata.kind=solved-route` 知识项。
- failed run 或包含非零 exit/timeout 命令的 run 生成 `metadata.kind=failure-retrospective` 知识项。
- 失败复盘记录 `wrong_hypotheses`、`invalid_commands`、`next_suggestions`。
- 所有知识项入库时强制 `source_run` 非空。

新增 CLI：

```bash
ctf-agent memory add --category misc --pattern "text file contains flag" --symptom "local text inspection" --solution "read declared files" --command "cat prompt.txt" --source-run ~/ctf-workspace/runs/challenge1
ctf-agent memory search "text flag" --category misc
ctf-agent memory learn ~/ctf-workspace/runs/challenge1
```

测试覆盖：

- MemoryStore 拒绝无 `source_run` 的知识项。
- add/search roundtrip。
- 从 solved run 学习有效路线。
- 从 failed run 学习失败复盘。
- 学习时忽略早于当前 state 创建时间的旧 trace，避免复用 run 目录造成历史污染。
- Planner 在 planning 前检索 memory。
- Orchestrator solved 后自动学习。
- CLI memory add/search。

实际验证：

```text
. .venv/bin/activate && make test
103 passed in 3.95s

. .venv/bin/activate && make doctor
CTF Agent Environment Doctor
OK: True
Docker smoke attempted=True ok=True

CTF_AGENT_WORKSPACE_DIR=~/ctf-workspace-stage10 CTF_AGENT_MEMORY_PATH=~/ctf-workspace-stage10/memory/knowledge.sqlite ctf-agent solve examples/challenge1 --executor local --max-steps 10 --json
state: solved
flag: flag{example_only}
learned: 1 solved-route item

CTF_AGENT_WORKSPACE_DIR=~/ctf-workspace-stage10 CTF_AGENT_MEMORY_PATH=~/ctf-workspace-stage10/memory/knowledge.sqlite ctf-agent memory search "Example text flag" --category misc --limit 3
source_run=/home/liuxinyue/ctf-workspace-stage10/runs/challenge1

CTF_AGENT_WORKSPACE_DIR=~/ctf-workspace-stage10 CTF_AGENT_MEMORY_PATH=~/ctf-workspace-stage10/memory/knowledge.sqlite ctf-agent memory learn ~/ctf-workspace-stage10/runs/challenge1
learned: 1

CTF_AGENT_WORKSPACE_DIR=~/ctf-workspace-stage10 CTF_AGENT_MEMORY_PATH=~/ctf-workspace-stage10/memory/knowledge.sqlite ctf-agent memory add --category misc --pattern "manual text triage" --symptom "small text challenge" --solution "read declared files first" --command "cat prompt.txt" --source-run ~/ctf-workspace-stage10/runs/challenge1
added: 08031ad22d2b4a86a3134f21944c6ac7

trace.jsonl contains action=memory-search and plan metadata contains memory_matches=1.
```

许可证边界复核：

- 当前阶段没有引入第三方项目源码。
- SQLite 使用 Python 标准库。
- AGPL/GPL 参考项目继续只借设计，不复制代码、配置、提示词、文档或测试数据。

## 阶段 11 实施记录

更新日期：2026-08-31

阶段 11 已完成 evals：

- `ctf_agent.evals.base`
  - `BenchmarkAdapter`
  - `BenchmarkChallenge`
  - `CybenchAdapter`
  - `NYUCTFBenchAdapter`
  - `CyberZeroAdapter`
- `ctf_agent.evals.local`
  - `LocalBenchmark`
- `ctf_agent.evals.runner`
  - `BenchmarkRunner`
  - `EvalChallengeResult`
  - `EvalSummary`
  - `render_eval_report`

本地数据集：

- `evals/datasets/local/crypto-basic`
- `evals/datasets/local/forensics-basic`
- `evals/datasets/local/web-basic`

指标：

- `solved_count`
- `steps_used`
- `time_used`
- `command_count`
- `verifier_false_positive`
- `resume_success`

新增 CLI：

```bash
ctf-agent eval ./evals/datasets/local --max-steps 20
ctf-agent eval ./evals/datasets/local --executor local --output-dir ~/ctf-workspace/evals/local-smoke
```

输出文件：

- `eval_report.md`
- `eval_results.jsonl`
- `eval_summary.json`

测试覆盖：

- `LocalBenchmark` 读取本地数据集和 expected flags。
- `BenchmarkRunner` 写入 `eval_report.md` 和 `eval_results.jsonl`。
- false positive 指标。
- CLI `ctf-agent eval`。
- Cybench、NYU CTF Bench、Cyber-Zero placeholder 行为。

实际验证：

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

许可证边界复核：

- BoxPwnr 只借鉴 benchmark harness 思想，不复制代码。
- Cybench、NYU CTF Bench、Cyber-Zero 仅预留适配器接口。
- 当前阶段没有联网、下载或引入外部 benchmark 数据。
- AGPL/GPL 项目继续只借设计，不复制代码、配置、提示词、文档或测试数据。

## 阶段 12 实施记录

更新日期：2026-08-31

阶段 12 已完成轻量 Web UI：

- `ctf_agent.ui.server`
  - Python 标准库 HTTP 服务
  - 本地单页工作台
  - JSON API
- `ctf-agent ui`

页面包括：

- challenge 列表
- run 状态
- trace 时间线
- 文件/artifact 浏览
- flag candidates
- writeup 预览
- 手动确认提交按钮

新增 API：

- `GET /api/health`
- `GET /api/challenges`
- `GET /api/runs`
- `GET /api/runs/<run_id>`
- `GET /api/runs/<run_id>/trace`
- `GET /api/runs/<run_id>/files`
- `GET /api/runs/<run_id>/file?path=...`
- `GET /api/runs/<run_id>/writeup?generate=true`
- `POST /api/runs/<run_id>/submit`

安全边界：

- 默认绑定 `127.0.0.1:8008`。
- submit API 默认 dry-run。
- 真实提交必须 `submit=true` 且 `confirm=SUBMIT`。
- 文件读取限制在 run directory 内。
- 后端复用现有 core/CLI 逻辑，不破坏 CLI 行为。
- GPL 参考项目只借鉴工作台体验，不复制代码、样式、配置、提示词或文档。

测试覆盖：

- 首页工作台 HTML。
- `/api/health`。
- challenge 列表。
- run 状态。
- trace 时间线。
- 文件浏览和文件预览。
- writeup 预览。
- submit API 默认 dry-run。
- 真实提交需要 `confirm=SUBMIT`。

实际验证：

```text
. .venv/bin/activate && python -m pytest tests/test_ui.py -q
3 passed

. .venv/bin/activate && make test
111 passed in 5.16s

. .venv/bin/activate && make doctor
CTF Agent Environment Doctor
OK: True
Docker smoke attempted=True ok=True

CTF_AGENT_WORKSPACE_DIR=~/ctf-workspace-stage12 CTF_AGENT_MEMORY_ENABLED=false ctf-agent solve examples/challenge1 --executor local --max-steps 10
state: solved
flag: flag{example_only}

CTF_AGENT_WORKSPACE_DIR=~/ctf-workspace-stage12 CTF_AGENT_MEMORY_ENABLED=false ctf-agent ui --host 127.0.0.1 --port 8765 --challenges examples
html_has_workbench True
health_ok True
runs ['challenge1']
trace_events 5
writeup_has_flag True
```

## 工程成熟化加固记录

更新日期：2026-08-31

已完成：

- 初始化 git 仓库，但不创建 commit。
- 完善 `.gitignore`，排除 `.venv`、`__pycache__`、`.pytest_cache`、`*.egg-info`、build/dist、trace/eval/run artifacts 和本地密钥配置。
- 增强 WSL doctor：
  - `python` 缺失时建议使用 `python3`、激活 `.venv` 或安装 `python-is-python3`。
  - `rg` 缺失时建议安装 `ripgrep`。
  - `gh` 缺失时提示 GitHub CLI 为可选工作流工具。
  - `uv` 缺失时提示它是可选 Python 包管理器。
  - 可选工具缺失不阻塞 doctor。
- 增加 Makefile 命令：
  - `make test`
  - `make doctor`
  - `make eval-local`
  - `make lint-basic`
  - `make clean-generated`
- 增加 `scripts/bootstrap_wsl.sh`：
  - 创建 `.venv`
  - 安装 dev 依赖
  - 打印系统工具安装建议
  - 不强制安装 apt 包
- 增加 `scripts/lint_basic.py`：
  - 标准库 AST 语法检查
  - trailing whitespace 检查
  - tab indentation 检查
  - final newline 检查
- 增加 `docs/operations.md`：
  - 比赛前检查清单
  - 比赛中工作流
  - 赛后复盘流程

实际验证：

```text
. .venv/bin/activate && make lint-basic
Basic lint OK: 70 Python files checked

. .venv/bin/activate && make doctor
CTF Agent Environment Doctor
OK: True
rg suggestion: Recommended for fast source and artifact search. Install: sudo apt install ripgrep
gh suggestion: Optional GitHub workflow helper. Install: sudo apt install gh, or see https://cli.github.com/

. .venv/bin/activate && make test
112 passed in 5.21s

. .venv/bin/activate && make eval-local
solved_count: 3/3
steps_used: 6
command_count: 6
verifier_false_positive: 0
resume_success: 3
```

## 阶段 8 实施记录

更新日期：2026-08-31

阶段 8 已完成多专家协作：

- `ctf_agent.agents.classifier`
  - `CategoryClassifier`
  - `CategoryClassification`
- `ctf_agent.agents.specialists`
  - `SpecialistAgent`
  - `PwnAgent`
  - `WebAgent`
  - `CryptoAgent`
  - `RevAgent`
  - `ForensicsAgent`
  - `specialist_for_category`
- `ctf_agent.agents.critic`
  - `CriticAgent`
- `ctf_agent.agents.message_bus`
  - `AgentMessage`
  - `AgentMessageBus`

分类依据：

- challenge category metadata
- title / description / hints / connection 关键词
- connection 类型
- 附件扩展名
- 文件 magic bytes

Orchestrator 模式：

```text
single                  使用 PlannerAgent
specialist              分类后路由 SpecialistAgent
critic-after-failures   specialist 失败达到阈值后调用 CriticAgent
```

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

新增 CLI 参数：

```bash
ctf-agent solve examples/challenge1 --mode specialist
ctf-agent solve examples/challenge1 --mode critic-after-failures --critic-after-failures 1
ctf-agent resume ~/ctf-workspace/runs/challenge1 --mode specialist
```

专家约束：

- specialist 必须通过 `ToolRegistry.recommend(category)` 选择工具。
- specialist 只生成 Plan。
- 命令执行必须经过 Executor。
- 缺工具时保留 Python fallback。

AgentMessageBus 共享：

- `hypothesis`
- `observation`
- `flag_candidate`
- `failure_reason`

实际验证：

```text
. .venv/bin/activate && ctf-agent solve examples/challenge1 --executor local --mode specialist --max-steps 10
challenge: challenge1
state: solved
run_dir: /home/liuxinyue/ctf-workspace/runs/challenge1
steps_executed: 3
flag: flag{example_only}

. .venv/bin/activate && ctf-agent solve examples/challenge1 --executor local --mode critic-after-failures --critic-after-failures 1 --max-steps 10
challenge: challenge1
state: solved
run_dir: /home/liuxinyue/ctf-workspace/runs/challenge1
steps_executed: 3
flag: flag{example_only}

. .venv/bin/activate && pytest
86 passed in 3.37s
```

许可证边界复核：

- 当前阶段仍未复制任何参考项目源码。
- AGPL/GPL 项目继续只借设计，不直接复制代码、配置、提示词、文档或测试数据。

## 阶段 7 实施记录

更新日期：2026-08-31

阶段 7 已加入可配置 LLM 决策层，并保持无 LLM fallback：

- `ctf_agent.llm.provider`
  - `LLMProvider`
  - `LLMMessage`
  - `LLMResponse`
  - `OpenAICompatibleProvider`
  - `DummyProvider`
  - `build_provider`
- `ctf_agent.llm.prompts`
  - `PromptStore`
  - `render_template`
- `ctf_agent.llm.actions`
  - `parse_json_object`
  - `extract_command_actions`

配置：

```yaml
llm:
  provider: none
```

环境变量：

```bash
CTF_AGENT_LLM_PROVIDER=openai-compatible
OPENAI_BASE_URL=https://example.invalid/v1
OPENAI_API_KEY=replace-with-your-key
OPENAI_MODEL=replace-with-model-name
```

新增 prompts：

- `prompts/planner.md`
- `prompts/executor.md`
- `prompts/verifier.md`
- `prompts/reporter.md`
- `prompts/specialist_pwn.md`
- `prompts/specialist_web.md`
- `prompts/specialist_crypto.md`
- `prompts/specialist_rev.md`
- `prompts/specialist_forensics.md`

Prompt 统一约束：

- 模型只能输出结构化 JSON action。
- 不伪造 stdout、stderr、文件、flag 或工具结果。
- 每次只给少量命令。
- LLM 只决策，命令必须经过 Executor。
- 不提交 flag。
- 不针对未授权目标。

Agent 集成：

- `PlannerAgent` 优先尝试 LLM JSON plan，失败或空 plan 时 deterministic fallback。
- `VerifierAgent` 先正则提取；如需 LLM 标注候选，只接受确实出现在 observation 中的值。
- `Orchestrator` 从 config/env 构建 provider；默认 `llm.provider: none`。

实际验证：

```text
. .venv/bin/activate && ctf-agent solve examples/challenge1 --max-steps 10
challenge: challenge1
state: solved
run_dir: /home/liuxinyue/ctf-workspace/runs/challenge1
steps_executed: 2
flag: flag{example_only}

. .venv/bin/activate && CTF_AGENT_LLM_PROVIDER=dummy ctf-agent solve examples/challenge1 --executor local --max-steps 10 --json
"solved": true
"flag{example_only}"

. .venv/bin/activate && pytest
77 passed in 2.76s
```

许可证边界复核：

- 当前阶段仍未复制任何参考项目源码。
- AGPL/GPL 项目继续只借设计，不直接复制代码、配置、提示词、文档或测试数据。

## 阶段 6 实施记录

更新日期：2026-08-31

阶段 6 已完成无需真实 LLM 的 MVP 解题循环：

- `ctf_agent.agents.base`
  - `Agent`
  - `AgentContext`
- `ctf_agent.agents.planner`
  - `PlannerAgent`
  - `Plan`
  - `PlanCommand`
- `ctf_agent.agents.executor`
  - `ExecutorAgent`
  - `ExecutionBatch`
- `ctf_agent.agents.verifier`
  - `VerifierAgent`
  - `VerificationResult`
- `ctf_agent.core.orchestrator`
  - `Orchestrator`
  - `SolveResult`

MVP loop：

```text
new
  -> analyzing   PlannerAgent 生成确定性 plan
  -> running     ExecutorAgent 执行 plan
  -> verifying   VerifierAgent 提取 flag 候选
  -> solved      找到 verified flag
  -> failed      未找到 flag 或空 plan
```

Planner 当前策略：

- 读取 challenge description 和 files。
- 查询工具注册表的 category 推荐。
- 使用 `python3` 生成文件文本扫描命令。
- 不依赖真实 LLM。
- 不依赖完整 CTF 工具链，默认 Docker `ctf-agent:generic` profile 也能运行。

Verifier 当前策略：

- 从 stdout/stderr 和对应 artifact 中提取 flag。
- 优先使用 `challenge.flag_regex`。
- 追加默认 `flag{...}` / `FLAG{...}` / `ctf{...}` / `CTF{...}` pattern。
- 正则命中即作为 MVP verified candidate。
- 不提交 flag，`submitted=False`。

新增 CLI：

```bash
ctf-agent solve examples/challenge1 --max-steps 10
ctf-agent resume ~/ctf-workspace/runs/challenge1
```

实际验证：

```text
. .venv/bin/activate && ctf-agent solve examples/challenge1 --max-steps 10
challenge: challenge1
state: solved
run_dir: /home/liuxinyue/ctf-workspace/runs/challenge1
steps_executed: 2
flag: flag{example_only}

. .venv/bin/activate && ctf-agent resume ~/ctf-workspace/runs/challenge1
challenge: challenge1
state: solved
run_dir: /home/liuxinyue/ctf-workspace/runs/challenge1
steps_executed: 0
flag: flag{example_only}

. .venv/bin/activate && pytest
65 passed in 3.13s
```

Toy challenge：

- `examples/challenge1/challenge.yaml`
- `examples/challenge1/prompt.txt`
- flag：`flag{example_only}`

生成文件：

```text
~/ctf-workspace/runs/challenge1/state.json
~/ctf-workspace/runs/challenge1/trace.jsonl
~/ctf-workspace/runs/challenge1/work/prompt.txt
~/ctf-workspace/runs/challenge1/artifacts/command-output/*.stdout.txt
~/ctf-workspace/runs/challenge1/artifacts/command-output/*.stderr.txt
```

许可证边界复核：

- 当前阶段仍未复制任何参考项目源码。
- AGPL/GPL 项目继续只借设计，不直接复制代码、配置、提示词、文档或测试数据。

## 阶段 5 实施记录

更新日期：2026-08-31

阶段 5 已完成 tools 层：

- `ctf_agent.tools.spec`
  - `ToolSpec`
  - `RiskLevel`
- `ctf_agent.tools.registry`
  - `ToolRegistry`
- `ctf_agent.tools.builtin`
  - `builtin_tools`
  - `default_registry`
- `ctf_agent.tools.doctor`
  - `ToolCheck`
  - `check_tool`
  - `build_tools_doctor`
  - `print_tools_doctor`

内置工具：

- generic：`file`、`strings`、`xxd`、`hexdump`、`rg`
- pwn：`checksec`、`gdb`、`pwntools`
- rev：`readelf`、`objdump`、`radare2`、`angr`
- crypto：`python`、`sage`、`z3`、`RsaCtfTool`
- web：`curl`、`nmap`、`sqlmap`、`ffuf`、`playwright`
- forensics：`binwalk`、`exiftool`、`foremost`、`zsteg`

ToolSpec 字段：

- `name`
- `category`
- `description`
- `command_template`
- `inputs`
- `risk_level`
- `required_bins`
- `install_hint`
- `metadata`

Doctor 行为：

- 检测 `required_bins`。
- `python` / `python3` 可回退到当前 `sys.executable`，避免未激活 `.venv` 时误判。
- 对 `metadata.python_package` 额外检测 Python import。
- 缺工具不崩溃，返回码保持 0。
- 输出安装建议。

新增 CLI：

```bash
ctf-agent tools list
ctf-agent tools list --category generic
ctf-agent tools list --query debugger --json
ctf-agent tools doctor
ctf-agent tools doctor --category web
ctf-agent tools doctor --json
```

实际验证：

```text
. .venv/bin/activate && pytest
58 passed in 3.16s

. .venv/bin/activate && ctf-agent tools doctor
CTF Agent Tools Doctor
OK: True available=13 missing=12 total=25
```

当前缺失项包括 `rg`、`sage`、`z3`、`pwntools`、`angr`、`radare2`、`ffuf`、`playwright`、`exiftool`、`foremost`、`zsteg`、`RsaCtfTool.py`。这些缺失不会阻止 agent 启动，后续可以按题型逐步安装。

许可证边界复核：

- 当前阶段仍未复制任何参考项目源码。
- AGPL/GPL 项目继续只借设计，不直接复制代码、配置、提示词、文档或测试数据。

## 阶段 4 实施记录

更新日期：2026-08-31

阶段 4 已完成 WSL 主执行器和 Docker sandbox：

- `ctf_agent.sandbox.executor`
  - `Executor`
  - `ExecutionResult`
  - `LocalExecutor`
  - `WorkspaceBoundaryError`
  - `CommandSafetyError`
- `ctf_agent.sandbox.docker`
  - `DockerExecutor`
  - `image_for_category`
  - `docker_available`

执行接口：

```python
run(command, cwd, timeout, env)
```

安全策略：

- `LocalExecutor` 要求 `cwd` 位于 workspace root 内。
- 默认拒绝 workspace 外破坏性操作。
- 覆盖的破坏性命令包括 `rm`、`rmdir`、`unlink`、`shred`、`truncate`、`dd`、`mkfs`、`mkswap`、`mount`、`umount`、`mv`。
- Docker executor 将 workspace root mount 到容器 `/workspace`，并把 cwd 映射到对应容器路径。

Trace 与 artifact：

- 每次执行写入 `trace.jsonl`。
- trace 记录 `agent`、`action`、`command`、stdout/stderr 摘要、artifact、exit_code、started_at、ended_at、duration、timeout、env、cwd。
- 完整 stdout/stderr 写入 `artifacts/command-output/`。

Docker 镜像配置：

```yaml
sandbox:
  engine: docker
  default_profile: generic
  images:
    generic: ctf-agent:generic
    pwn: ctf-agent:pwn
    web: ctf-agent:web
    crypto: ctf-agent:crypto
    rev: ctf-agent:rev
    forensics: ctf-agent:forensics
```

新增 CLI：

```bash
ctf-agent exec <challenge_dir> -- "file ./binary"
ctf-agent doctor executors
```

Docker 不可用时：

- Docker 集成测试自动 skip。
- 默认 Docker executor 自动降级到 LocalExecutor。
- 显式 `--executor docker` 返回清晰错误，不抛 traceback。

实际验证：

```text
. .venv/bin/activate && pytest
49 passed in 2.80s

. .venv/bin/activate && ctf-agent doctor executors
CTF Agent Executor Doctor
OK: True
- local: ok workspace-boundary=enforced
- docker: available=True network=none memory=512m cpu=1.0

. .venv/bin/activate && ctf-agent exec examples/challenge1 --executor local -- "cat ./prompt.txt"
Welcome to the local platform adapter example.

The demo flag shape is flag{example_only}; do not submit it anywhere.

. .venv/bin/activate && ctf-agent exec examples/challenge1 -- "cat ./prompt.txt"
Welcome to the local platform adapter example.

The demo flag shape is flag{example_only}; do not submit it anywhere.
```

许可证边界复核：

- 当前阶段仍未复制任何参考项目源码。
- AGPL/GPL 项目继续只借设计，不直接复制代码、配置、提示词、文档或测试数据。

## 阶段 3 实施记录

更新日期：2026-08-31

阶段 3 已完成 platforms 层：

- `ctf_agent.platforms.base`
  - `PlatformAdapter`
  - `SubmissionResult`
- `ctf_agent.platforms.local`
  - `LocalPlatformAdapter`
  - 支持 `challenge.yaml` / `challenge.yml`
  - 支持目录推断 challenge
  - 支持复制题目文件到指定 destination
  - 本地 submit 始终 dry-run
- `ctf_agent.platforms.ctfd`
  - `CTFdPlatformAdapter`
  - `url` / `token` 配置入口
  - 可注入 transport 便于 mock 测试
  - list/detail/download/submit 骨架
  - submit 默认 dry-run

`challenge.yaml` 示例字段：

```yaml
title: Example Challenge 1
category: misc
description: A tiny local challenge fixture for platform adapter smoke tests.
files:
  - prompt.txt
connection:
flag_regex: flag\{[A-Za-z0-9_]+\}
```

新增 CLI：

```bash
ctf-agent list examples/
ctf-agent inspect examples/challenge1
```

实际验证：

```text
. .venv/bin/activate && ctf-agent list examples/
challenge1	misc	Example Challenge 1

. .venv/bin/activate && ctf-agent inspect examples/challenge1
{
  "category": "misc",
  "connection": null,
  "description": "A tiny local challenge fixture for platform adapter smoke tests.",
  "files": [
    "prompt.txt"
  ],
  "flag_regex": "flag\\{[A-Za-z0-9_]+\\}",
  "hints": [],
  "id": "challenge1",
  "metadata": {
    "source": "local",
    "source_dir": "examples/challenge1"
  },
  "title": "Example Challenge 1"
}

. .venv/bin/activate && pytest
35 passed in 0.74s
```

许可证边界复核：

- 当前阶段仍未复制任何参考项目源码。
- AGPL/GPL 项目继续只借设计，不直接复制代码、配置、提示词、文档或测试数据。
