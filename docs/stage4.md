# Stage 4 Notes

阶段 4 实现 WSL 主执行器和 Docker sandbox。

## Executor 接口

新增 `ctf_agent.sandbox.executor`：

- `Executor.run(command, cwd, timeout, env)`
- `ExecutionResult`
- `LocalExecutor`
- `WorkspaceBoundaryError`
- `CommandSafetyError`

`ExecutionResult` 记录：

- `command`
- `cwd`
- `env`
- `timeout`
- `exit_code`
- `stdout`
- `stderr`
- `started_at`
- `ended_at`
- `duration_seconds`
- `timed_out`
- `artifacts`
- `metadata`

## LocalExecutor

`LocalExecutor` 使用 WSL 本地 `/bin/bash -lc` 执行命令。

安全策略：

- `cwd` 必须位于 workspace root 内。
- 空命令拒绝执行。
- 对 `rm`、`rmdir`、`unlink`、`shred`、`truncate`、`dd`、`mkfs`、`mkswap`、`mount`、`umount`、`mv` 等破坏性命令做路径检查。
- 默认拒绝破坏 workspace 外路径。

## DockerExecutor

新增 `ctf_agent.sandbox.docker.DockerExecutor`：

- 使用 WSL 内 Docker CLI。
- 将 workspace root mount 到容器 `/workspace`。
- 容器 `--workdir` 映射到当前 challenge workspace 子目录。
- 支持 `--network`、`--memory`、`--cpus`。
- 使用 `timeout <seconds>s <command>` 约束容器内命令。

Docker 不可用时：

- 测试中的 Docker 集成测试会 skip。
- CLI 默认 Docker executor 会优雅降级到 LocalExecutor。
- 用户显式 `--executor docker` 时，返回清晰错误，不输出 traceback。

## 镜像配置

`configs/default.yaml` 支持按题型配置镜像：

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

当前所有 profile 先使用同一个轻量 Python 镜像，后续阶段再替换为带工具链的专用镜像。

## Trace 与 Artifact

命令执行后：

- 完整 stdout 写入 `artifacts/command-output/*.stdout.txt`。
- 完整 stderr 写入 `artifacts/command-output/*.stderr.txt`。
- trace JSONL 只保留 stdout/stderr 摘要。
- trace metadata 记录 executor、cwd、env、timeout、timed_out、duration_seconds。

## CLI

新增命令：

```bash
ctf-agent exec <challenge_dir> -- "file ./binary"
ctf-agent doctor executors
```

支持：

```bash
ctf-agent exec examples/challenge1 -- "cat ./prompt.txt"
ctf-agent exec examples/challenge1 --executor local -- "cat ./prompt.txt"
ctf-agent exec examples/challenge1 --timeout 5 --env DEMO=1 -- "env | grep DEMO"
```

实际验证：

```text
ctf-agent doctor executors
CTF Agent Executor Doctor
OK: True
- local: ok workspace-boundary=enforced
- docker: available=True network=none memory=512m cpu=1.0

ctf-agent exec examples/challenge1 --executor local -- "cat ./prompt.txt"
Welcome to the local platform adapter example.

The demo flag shape is flag{example_only}; do not submit it anywhere.

ctf-agent exec examples/challenge1 -- "cat ./prompt.txt"
Welcome to the local platform adapter example.

The demo flag shape is flag{example_only}; do not submit it anywhere.
```

## 测试

新增测试覆盖：

- LocalExecutor 正常执行。
- cwd workspace 边界。
- workspace 外破坏性命令拒绝。
- workspace 内破坏性命令允许。
- timeout 记录。
- stdout/stderr trace 摘要和完整 artifact。
- Docker 命令构造。
- Docker 集成 smoke，不可用或镜像缺失时 skip。
- CLI `exec`、`doctor executors`、Docker 降级。

验证结果：

```text
pytest
49 passed in 2.80s
```
