# Stage 2 Notes

阶段 2 实现 core 数据模型、状态机、workspace 管理、JSONL trace 和 resume 基础能力。

## 数据模型

新增 `ctf_agent.core.models`：

- `Challenge`
  - `id`
  - `title`
  - `category`
  - `description`
  - `files`
  - `connection`
  - `hints`
  - `flag_regex`
  - `metadata`
- `Attempt`
- `Step`
- `Observation`
- `Artifact`
- `FlagCandidate`

所有模型都提供 `to_dict()` / `from_dict()`，保持 JSON 友好，便于 trace、state 和未来平台 adapter 复用。

## 状态机

新增 `ctf_agent.core.state`：

- `ChallengeState.NEW`
- `ChallengeState.ANALYZING`
- `ChallengeState.RUNNING`
- `ChallengeState.VERIFYING`
- `ChallengeState.SOLVED`
- `ChallengeState.FAILED`
- `ChallengeState.PAUSED`

`ChallengeRunState.transition_to()` 会校验状态转换，不允许从 `solved` 继续运行。`failed` 允许回到 `analyzing`，用于人工修复配置或补充线索后的恢复。

## Workspace

新增 `ctf_agent.core.workspace.WorkspaceManager`：

- 默认根目录：`~/ctf-workspace`
- 每题目录：`~/ctf-workspace/runs/<challenge_id>/`
- 子目录：
  - `input/`
  - `work/`
  - `artifacts/`
- 状态文件：`state.json`
- trace 文件：`trace.jsonl`

`WorkspaceManager.resume(challenge_id)` 会同时恢复 `state.json` 和 `trace.jsonl`。

## Trace

新增 `ctf_agent.core.trace.TraceStore`：

- 每步追加 JSONL。
- 记录 `challenge_id`、`agent`、`action`、`command`、`stdout` 摘要、`stderr` 摘要、`artifacts`、`exit_code`、`started_at`、`ended_at`、`timestamp`、`metadata`。
- 默认 stdout/stderr 摘要上限为 4000 字符，避免 trace 文件膨胀。

## 测试

新增测试：

- `tests/test_core_models.py`
- `tests/test_state.py`
- `tests/test_trace_workspace.py`

验证结果：

```text
pytest
23 passed in 0.72s
```
