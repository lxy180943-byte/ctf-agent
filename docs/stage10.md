# Stage 10: Memory

更新日期：2026-08-31

## Scope

阶段 10 引入可追溯的本地知识库，让 agent 从 solved run 和 failed run 中积累经验。

## Implemented

- `KnowledgeItem`
  - `category`
  - `pattern`
  - `symptom`
  - `solution`
  - `commands`
  - `source_run`
  - `confidence`
- `MemoryStore`
  - 使用标准库 SQLite，默认路径为 `~/ctf-workspace/memory/knowledge.sqlite`
  - `add`
  - `search`
  - `learn_from_run`
  - 入库时强制 `source_run` 非空
- CLI
  - `ctf-agent memory search`
  - `ctf-agent memory add`
  - `ctf-agent memory learn`
- Planner
  - 新题开始 planning 前检索相关经验
  - 匹配项写入 trace 和 plan metadata
  - LLM planner prompt 增加 `Relevant memory JSON`
- Orchestrator
  - solved run 自动学习有效路线
  - failed run 或包含无效命令的 run 自动记录失败复盘

## Configuration

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

## CLI

```bash
ctf-agent memory add \
  --category misc \
  --pattern "text file contains flag" \
  --symptom "description hints at local text inspection" \
  --solution "read declared files and scan stdout for flag regex" \
  --command "cat prompt.txt" \
  --source-run ~/ctf-workspace/runs/challenge1

ctf-agent memory search "text flag" --category misc
ctf-agent memory learn ~/ctf-workspace/runs/challenge1
```

## Failure Review

失败复盘知识项使用 `metadata.kind=failure-retrospective`，包含：

- `wrong_hypotheses`
- `invalid_commands`
- `next_suggestions`

所有复盘都保留 `source_run`，可以从知识项回到原始 `state.json`、`trace.jsonl` 和 artifacts。

## Tests

新增 `tests/test_memory.py`，覆盖：

- `MemoryStore` 拒绝无 `source_run` 的知识项
- add/search roundtrip
- 从 solved run 学习有效路线
- 从 failed run 学习失败复盘
- 学习时忽略早于当前 state 创建时间的旧 trace
- Planner 在 planning 前检索 memory
- Orchestrator solved 后自动学习
- CLI memory add/search

最终验证结果：

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

ctf-agent memory search "Example text flag" --category misc --limit 3
source_run=/home/liuxinyue/ctf-workspace-stage10/runs/challenge1

ctf-agent memory learn ~/ctf-workspace-stage10/runs/challenge1
learned: 1

ctf-agent memory add --category misc --pattern "manual text triage" --symptom "small text challenge" --solution "read declared files first" --command "cat prompt.txt" --source-run ~/ctf-workspace-stage10/runs/challenge1
added: 08031ad22d2b4a86a3134f21944c6ac7

trace.jsonl contains action=memory-search and plan metadata contains memory_matches=1.
```

## Safety Notes

- Memory 只保存本地经验，不执行命令。
- Planner 只检索并引用 memory，不绕过 Executor。
- 知识项必须有 `source_run`，不接受无来源经验。
- GPL/AGPL 参考项目只借鉴设计，没有复制源码、配置、提示词、文档或测试数据。
