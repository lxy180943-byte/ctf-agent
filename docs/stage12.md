# Stage 12: Local Web Workbench

更新日期：2026-08-31

## Scope

阶段 12 在不影响 CLI 的前提下加入轻量本地 Web UI。当前实现使用 Python 标准库 `http.server`，避免新增运行依赖。

## Implemented

- `ctf_agent.ui.server`
  - 本地 HTTP 服务
  - JSON API
  - 单页工作台 HTML/CSS/JS
- CLI
  - `ctf-agent ui`
  - `ctf-agent ui --host 127.0.0.1 --port 8008 --challenges examples`

配置：

```yaml
ui:
  challenge_root: examples
  host: 127.0.0.1
  port: 8008
```

## Workbench Views

- challenge 列表
- run 状态
- trace 时间线
- workspace/input/artifact 文件浏览
- flag candidates
- writeup 预览
- 手动确认提交按钮

UI 第一屏就是工作台，不包含营销页。布局使用三栏工具台：

- 左侧：Challenges 和 Runs
- 中间：Run 状态、Trace、Files、Writeup
- 右侧：Flag candidates 和 submit/dry-run 控制

## API

- `GET /`
- `GET /api/health`
- `GET /api/challenges?path=examples`
- `GET /api/runs`
- `GET /api/runs/<run_id>`
- `GET /api/runs/<run_id>/trace`
- `GET /api/runs/<run_id>/files`
- `GET /api/runs/<run_id>/file?path=work/file.txt`
- `GET /api/runs/<run_id>/writeup?generate=true`
- `POST /api/runs/<run_id>/submit`

`POST /api/runs/<run_id>/submit` 默认 dry-run。真实提交必须传 `submit=true` 且 `confirm=SUBMIT`。

## Safety Notes

- Web UI 只绑定本地地址，默认 `127.0.0.1:8008`。
- 提交默认 dry-run。
- 真实提交需要手动勾选并输入 `SUBMIT`。
- 文件读取限制在 run directory 内，拒绝路径穿越。
- 后端复用现有 core/platform/memory/evals 代码，不改 CLI 既有行为。
- GPL 参考项目只借鉴工作台体验，没有复制源码、样式、配置、提示词或文档。

## Tests

新增 `tests/test_ui.py`，覆盖：

- 首页工作台 HTML
- `/api/health`
- challenge 列表
- run 状态
- trace 时间线
- 文件浏览和文件预览
- writeup 预览
- submit API 默认 dry-run
- 真实提交需要 `confirm=SUBMIT`

最终验证结果：

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
