# Specialist Triage Pipelines

Specialist agents now produce structured `hypothesis`, `evidence`, and `next_commands` metadata before execution. The metadata is written to trace events as `specialist-triage`, shared through the message bus, and rendered in `writeup.md`.

## Pipelines

- PwnAgent: `file`, `checksec`, `readelf`, `strings`, Python printable fallback, `solve.py` template with local/remote `nc` parameters, and `gdb-notes.md`.
- RevAgent: `file`, `strings`, magic family classifier, `readelf`, `objdump`, and light `r2` triage when available. ELF, PE, Python bytecode, and APK are identified or noted.
- CryptoAgent: primitive/encoding detection for RSA, base64, hex, XOR, small exponent, common modulus, LCG, and substitution clues, plus a runnable `solve.py` draft.
- WebAgent: challenge-scoped curl headers/body, robots check, bounded directory probes, harmless parameter fuzz, and local HTML form parsing.
- ForensicsAgent: `file`, `binwalk`, `exiftool`, `strings`, hexdump/xxd, and carved output under `../artifacts/forensics`.

All commands are still executed by the configured Executor and traced normally. Missing optional tools emit clear `missing tool` observations instead of crashing.
