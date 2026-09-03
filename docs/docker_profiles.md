# Docker Sandbox Profiles

更新日期：2026-08-31

## Goal

Docker 是 CTF Agent 的主 sandbox。镜像按题型拆分，避免把所有工具塞进一个巨大镜像。

## Profiles

- `ctf-agent:generic`
  - `python3`
  - `pip`
  - `file`
  - `binutils`
  - `curl`
  - `wget`
  - `ripgrep`
  - `jq`
  - `xxd`
  - `netcat-openbsd`
- `ctf-agent:pwn`
  - `gdb`
  - `gdbserver`
  - `checksec`
  - `pwntools`
  - `ROPgadget`
  - `pwntools` / `ROPgadget` 使用 Ubuntu package 安装，`one_gadget` 在 `/opt/ctf-agent/README.pwn-tools` 中作为可选说明
- `ctf-agent:web`
  - `curl`
  - `nmap`
  - `ffuf`
  - `sqlmap`
  - Python `requests` / `httpx`
- `ctf-agent:crypto`
  - Python
  - `sympy`
  - `pycryptodome`（Ubuntu package 导入名为 `Cryptodome`）
  - `python3-z3` / `z3`
  - Sage 拆为可选 profile，不并入 crypto 小镜像
- `ctf-agent:rev`
  - `binutils`
  - `rizin` 或 `radare2`
  - `gdb`
  - `strings`
  - `ltrace`
  - `strace`
- `ctf-agent:forensics`
  - `binwalk`
  - `exiftool`
  - `foremost`
  - `pngcheck`
  - `steghide`
  - `zsteg` 安装说明：`/opt/ctf-agent/README.zsteg`

## Build

```bash
make docker-build
make docker-build-generic
make docker-build-pwn
make docker-build-web
make docker-build-crypto
make docker-build-rev
make docker-build-forensics
```

Equivalent CLI:

```bash
ctf-agent docker build --profile all
ctf-agent docker build --profile crypto
```

## Doctor

```bash
ctf-agent docker doctor
ctf-agent docker doctor --run-tools
```

`--run-tools` executes profile-specific core commands inside each local image.

## Configuration

`configs/default.yaml` maps challenge categories to local images:

```yaml
sandbox:
  images:
    generic: ctf-agent:generic
    pwn: ctf-agent:pwn
    web: ctf-agent:web
    crypto: ctf-agent:crypto
    rev: ctf-agent:rev
    forensics: ctf-agent:forensics
    misc: ctf-agent:generic
    sage: sagemath/sagemath:latest
```

## Verification

Last local verification on 2026-08-31:

```bash
make docker-build
make docker-doctor
make lint-basic
make test
```

Result: all six local images built successfully, `ctf-agent docker doctor --run-tools` passed for every profile, basic lint passed, and pytest reported `117 passed`.

Notes from verification:

- `pwn`, `web`, and `crypto` use Ubuntu packages for their Python libraries where available, avoiding fragile PyPI downloads during competition bootstrap.
- Ubuntu `python3-pycryptodome` exposes the `Cryptodome` import name, so docker doctor verifies `import Cryptodome`.
- `xxd` and `pngcheck` help commands can return non-zero exit codes, so docker doctor checks their executable presence with stable shell checks.

## Safety

- Images are intended only for authorized CTF, local lab, competition, and benchmark use.
- Runtime Docker executor still applies workspace mount, timeout, memory, CPU, network, and destructive-command checks.
- Default sandbox network remains `none`.
