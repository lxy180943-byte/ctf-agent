# Stage 5 Notes

阶段 5 实现 tools 层：工具规格、注册表、内置工具清单、工具环境 doctor 和 CLI。

## ToolSpec

新增 `ctf_agent.tools.spec.ToolSpec`：

- `name`
- `category`
- `description`
- `command_template`
- `inputs`
- `risk_level`
- `required_bins`
- `install_hint`
- `metadata`

`risk_level` 当前支持：

- `low`
- `medium`
- `high`

## ToolRegistry

新增 `ctf_agent.tools.registry.ToolRegistry`：

- `register(tool)`
- `get(name)`
- `list(category=None)`
- `categories()`
- `query(text)`
- `recommend(category, limit=None)`

推荐策略当前是最小实现：优先返回同 category 工具；未知 category 回退到 `generic`。

## 内置工具

新增 `ctf_agent.tools.builtin.default_registry()`，内置以下工具：

- generic：`file`、`strings`、`xxd`、`hexdump`、`rg`
- pwn：`checksec`、`gdb`、`pwntools`
- rev：`readelf`、`objdump`、`radare2`、`angr`
- crypto：`python`、`sage`、`z3`、`RsaCtfTool`
- web：`curl`、`nmap`、`sqlmap`、`ffuf`、`playwright`
- forensics：`binwalk`、`exiftool`、`foremost`、`zsteg`

`pwntools`、`angr`、`z3`、`playwright`、`RsaCtfTool` 目前作为占位工具保留，用于后续 specialist 和工具链安装阶段接入。

## Tools Doctor

新增 `ctf_agent.tools.doctor`：

- 检测 `required_bins` 是否在 PATH 中。
- 当 required bin 是 `python` 或 `python3` 时，如果 PATH 不含对应命令，会回退到当前 `sys.executable`，以兼容 `make test` 这类未激活 `.venv` 但指定解释器的运行方式。
- 对 `metadata.python_package` 标记的工具额外检测 Python import。
- 缺工具不崩溃，输出安装建议。
- doctor 自身返回 `ok=True`，缺失项体现在 `missing` 和每个 check 中。

当前机器实际检测摘要：

```text
ctf-agent tools doctor
CTF Agent Tools Doctor
OK: True available=13 missing=12 total=25
```

部分缺失项和建议：

```text
generic/rg: sudo apt install ripgrep
crypto/sage: sudo apt install sagemath
crypto/z3: python -m pip install z3-solver
pwn/pwntools: python -m pip install pwntools
rev/angr: python -m pip install angr
rev/radare2: sudo apt install radare2
web/ffuf: sudo apt install ffuf
web/playwright: python -m pip install playwright && python -m playwright install
forensics/exiftool: sudo apt install libimage-exiftool-perl
forensics/foremost: sudo apt install foremost
forensics/zsteg: gem install zsteg
```

## CLI

新增命令：

```bash
ctf-agent tools list
ctf-agent tools list --category generic
ctf-agent tools list --query debugger --json
ctf-agent tools doctor
ctf-agent tools doctor --category web
ctf-agent tools doctor --json
```

## 测试

新增测试覆盖：

- `ToolSpec` 序列化。
- `ToolRegistry` 注册、查询、推荐。
- 内置工具和 category 完整性。
- 缺失 binary 不崩溃并输出安装建议。
- 缺失 Python package 不误报可用。
- CLI `tools list` / `tools doctor`。

验证结果：

```text
pytest
58 passed in 3.16s
```

许可证边界保持不变：当前阶段没有复制任何参考项目代码、提示词、配置、文档或测试数据。
