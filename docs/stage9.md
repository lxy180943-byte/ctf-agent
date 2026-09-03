# Stage 9: Verification And Artifact Output

更新日期：2026-08-31

## Scope

阶段 9 完成 flag 验证、dry-run 提交和 writeup 产物输出，保持本地专用和授权 CTF 边界。

## Implemented

- `FlagDetector`
  - 支持 challenge `flag_regex`
  - 支持常见 flag 格式
  - 支持 `verification.custom_flag_patterns`
  - 从文本、文件和 artifact 中提取候选
  - 按置信度、flag 值和来源去重排序
- `VerifierAgent`
  - 从 stdout/stderr 提取候选
  - 从 artifact 文件提取候选
  - 从 workspace 中 challenge 声明的文件提取候选
  - LLM verifier 只接受实际观察到的候选
- `Submitter`
  - local 平台 dry-run 只记录意图
  - local `--submit` 标记 run solved 并设置候选 submitted
  - CTFd 默认 dry-run，真实提交必须显式 `--submit`
  - 提交结果写入 `state.metadata.last_submit`
- `Reporter`
  - 输出 `writeup.md`
  - 包含题目信息、关键命令、失败路线、最终 flag 和复现步骤

## CLI

```bash
ctf-agent report ~/ctf-workspace/runs/challenge1
ctf-agent submit ~/ctf-workspace/runs/challenge1 --dry-run
ctf-agent submit ~/ctf-workspace/runs/challenge1 --submit
```

`ctf-agent submit` 默认等价于 dry-run。若同时传入 `--dry-run` 和 `--submit`，仍按 dry-run 处理。

## Safety Notes

- 不会默认真实提交 flag。
- CTFd 提交必须显式 `--submit`，并且需要配置 URL/token。
- local submit 只修改本地 run state，不触达外部平台。
- 报告只读取 run directory 中的 `state.json`、`trace.jsonl` 和记录的 artifacts。
- GPL/AGPL 参考项目只借鉴设计，没有复制源码、配置、提示词、文档或测试数据。

## Tests

新增 `tests/test_verification_output.py`，覆盖：

- challenge regex、常见格式、自定义格式的候选提取
- 文件和 artifact 提取
- verifier 从 workspace 文件提取
- local dry-run
- local `--submit`
- CTFd dry-run mock
- reporter writeup 生成

验证命令：

```bash
make test
ctf-agent solve examples/challenge1 --executor local --max-steps 10
ctf-agent report ~/ctf-workspace/runs/challenge1
ctf-agent submit ~/ctf-workspace/runs/challenge1 --dry-run
```

最终验证结果：

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
challenge: challenge1
platform: local
dry_run: True
submitted: False
accepted: True
flag: flag{example_only}
message: dry-run: local run would be marked solved
```
