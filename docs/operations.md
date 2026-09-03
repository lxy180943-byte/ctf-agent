# CTF Agent Operations

更新日期：2026-08-31

## 比赛前检查清单

- 确认项目和工作区在 WSL Linux 文件系统：
  - `/home/liuxinyue/ctf-agent`
  - `/home/liuxinyue/ctf-workspace`
  - `/home/liuxinyue/ctf-artifacts`
- 激活虚拟环境：
  - `. .venv/bin/activate`
- 跑基础检查：
  - `make doctor`
  - `make lint-basic`
  - `make test`
  - `make eval-local`
- 确认 Docker 可用：
  - `docker info`
  - `ctf-agent doctor executors`
- 构建或检查 Docker sandbox profiles：
  - `make docker-build-generic`
  - `make docker-build`
  - `make docker-doctor`
  - `ctf-agent docker doctor --run-tools`
- 确认缺失工具是否会影响本场题型：
  - `ctf-agent tools doctor`
  - `ctf-agent tools doctor --category web`
  - `ctf-agent tools doctor --category pwn`
- 检查配置：
  - `configs/default.yaml`
  - 必要时使用 `CTF_AGENT_WORKSPACE_DIR`
  - 不把高频 workspace 放到 `/mnt/c` 或 `/mnt/d`
- 明确提交策略：
  - 默认 dry-run
  - 真实提交必须使用 `--submit`
  - Web UI 真实提交必须输入 `SUBMIT`

## 比赛中工作流

- 导入题目：
  - 本地目录使用 `challenge.yaml`
  - 附件放在题目目录内，路径写入 `files`
- 快速查看：
  - `ctf-agent list <challenge_root>`
  - `ctf-agent inspect <challenge_dir>`
- 单题自动分析：
  - `ctf-agent solve <challenge_dir> --executor docker --max-steps 20`
  - Docker 不可用或镜像缺工具时使用 `--executor local`
  - pwn/web/crypto/rev/forensics 会按 category 选择对应 `ctf-agent:<profile>` 镜像
- 多专家模式：
  - `ctf-agent solve <challenge_dir> --mode specialist`
  - `ctf-agent solve <challenge_dir> --mode critic-after-failures --critic-after-failures 1`
- 查看过程：
  - `tail -f ~/ctf-workspace/runs/<id>/trace.jsonl`
  - `ctf-agent report ~/ctf-workspace/runs/<id>`
  - `ctf-agent ui --challenges <challenge_root>`
- GUI 工具交接：
  - 需要 IDA、010 Editor、Burp、Wireshark 时，把文件复制到 `~/ctf-artifacts`
  - Windows GUI 只处理明确导出的 artifact
- 提交：
  - 先 dry-run：`ctf-agent submit ~/ctf-workspace/runs/<id> --dry-run`
  - 确认后再真实提交：`ctf-agent submit ~/ctf-workspace/runs/<id> --submit`

## 赛后复盘流程

- 为 solved run 生成报告：
  - `ctf-agent report ~/ctf-workspace/runs/<id>`
- 写入知识库：
  - `ctf-agent memory learn ~/ctf-workspace/runs/<id>`
  - 手动补充：`ctf-agent memory add --category ... --pattern ... --symptom ... --solution ... --source-run ~/ctf-workspace/runs/<id>`
- 复盘 failed run：
  - 查看 `trace.jsonl`
  - 标记错误假设、无效命令、下次建议
  - 确保 memory item 有 `source_run`
- 跑本地 benchmark：
  - `make eval-local`
  - 查看 `~/ctf-workspace/evals/local-latest/eval_report.md`
- 清理生成物：
  - `make clean-generated`
- 版本化前检查：
  - `git status --short --ignored`
  - 确认 `.venv`、`__pycache__`、`.pytest_cache`、`*.egg-info`、run artifacts 没有进入暂存区
