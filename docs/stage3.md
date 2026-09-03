# Stage 3 Notes

阶段 3 实现 platforms 层，支持本地 challenge 导入，并加入 CTFd adapter 骨架。

## PlatformAdapter

新增 `ctf_agent.platforms.base`：

- `PlatformAdapter`
  - `list_challenges()`
  - `get_challenge(challenge_id)`
  - `download_files(challenge, destination)`
  - `submit_flag(challenge, flag, submit=False)`
- `SubmissionResult`

提交接口默认以 `submit=False` 工作，真实提交必须显式传入 `submit=True`。

## LocalPlatformAdapter

新增 `ctf_agent.platforms.local.LocalPlatformAdapter`：

- 可从包含 `challenge.yaml` 的单题目录导入。
- 可从包含多个子目录的根目录列出 challenge。
- 无 `challenge.yaml` 时，会按目录名和文件列表推断最小 challenge。
- `download_files()` 将题目文件复制到指定目录。
- `submit_flag()` 始终 dry-run，不对外提交。

支持的 `challenge.yaml` 示例：

```yaml
title: Example Challenge 1
category: misc
description: A tiny local challenge fixture for platform adapter smoke tests.
files:
  - prompt.txt
connection:
flag_regex: flag\{[A-Za-z0-9_]+\}
```

## CTFdPlatformAdapter

新增 `ctf_agent.platforms.ctfd.CTFdPlatformAdapter`：

- 接收 `url` 和 `token`。
- 使用可注入 transport，便于 mock 测试。
- 骨架接口覆盖：
  - `/api/v1/challenges`
  - `/api/v1/challenges/<id>`
  - `/api/v1/challenges/attempt`
- `submit_flag()` 默认 dry-run，不调用提交接口。

默认配置新增：

```yaml
platform:
  default: local
  ctfd:
    url:
    token:
```

## CLI

新增命令：

```bash
ctf-agent list examples/
ctf-agent inspect examples/challenge1
```

验证输出：

```text
ctf-agent list examples/
challenge1	misc	Example Challenge 1

ctf-agent inspect examples/challenge1
{
  "id": "challenge1",
  "title": "Example Challenge 1",
  "category": "misc",
  "files": [
    "prompt.txt"
  ]
}
```

## 测试

新增测试：

- `tests/test_platforms_local.py`
- `tests/test_platforms_ctfd.py`
- CLI `list` / `inspect` 测试
- YAML list 和空值解析测试

验证结果：

```text
pytest
35 passed in 0.74s
```

许可证边界保持不变：只借鉴参考项目设计，不复制 GPL/AGPL 项目源码、提示词、配置、文档或测试数据。
