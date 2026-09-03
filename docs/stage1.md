# Stage 1 Notes

阶段 1 建立 Python 项目骨架，并把阶段 0 的一次性 doctor 迁移为包内模块与 CLI 子命令。

已实现：

- `pyproject.toml`，包含 `ctf-agent` console script。
- `configs/default.yaml`，包含 workspace、artifacts、sandbox、submit、logging、models 的默认配置。
- `src/ctf_agent/` 包结构：`core`、`platforms`、`agents`、`sandbox`、`tools`、`memory`、`evals`、`cli`。
- `ctf-agent --help`、`ctf-agent version`、`ctf-agent doctor`。
- 默认配置加载，支持 `CTF_AGENT_CONFIG` 指定配置文件，并支持常用环境变量覆盖。
- console 日志初始化和 `JsonlTraceWriter` 预留。
- pytest 测试覆盖 CLI、配置、doctor、JSONL trace。
- 项目本地 `.venv` 用于规避 Ubuntu 24.04 的 PEP 668 系统 Python 保护。

配置覆盖示例：

```bash
CTF_AGENT_LOG_LEVEL=DEBUG ctf-agent doctor
CTF_AGENT_WORKSPACE_DIR=/home/liuxinyue/ctf-workspace ctf-agent doctor
CTF_AGENT_CONFIG__SANDBOX__TIMEOUT_SECONDS=120 ctf-agent doctor
```

安全约束保持不变：默认 dry-run，不针对未授权目标，GPL/AGPL 参考项目只借设计。

验证结果：

```text
ctf-agent --help
usage: ctf-agent [-h] [--config CONFIG] {version,doctor} ...

ctf-agent version
ctf-agent 0.1.0

pytest
12 passed in 0.76s

ctf-agent doctor
OK: True
Docker smoke attempted=True ok=True
```
