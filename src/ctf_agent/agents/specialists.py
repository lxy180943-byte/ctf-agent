from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from ctf_agent.agents.base import Agent, AgentContext
from ctf_agent.agents.planner import Plan, PlanCommand
from ctf_agent.core.trace import TraceEvent
from ctf_agent.tools import ToolSpec

_PROJECT_SRC = str(Path(__file__).resolve().parents[2])


@dataclass
class TriagePipeline:
    category: str
    hypothesis: str
    evidence: list[str] = field(default_factory=list)
    next_commands: list[PlanCommand] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "hypothesis": self.hypothesis,
            "evidence": list(self.evidence),
            "next_commands": [command.to_dict() for command in self.next_commands],
            "notes": list(self.notes),
        }


class SpecialistAgent(Agent):
    category = "misc"

    def __init__(self, name: str, role: str, category: str) -> None:
        super().__init__(name=name, role=role)
        self.category = category

    def select_tools(self, context: AgentContext) -> list[ToolSpec]:
        return context.tool_registry.recommend(self.category, limit=10)

    def support_tools(self, context: AgentContext) -> list[ToolSpec]:
        return context.tool_registry.list("generic")

    def run(self, context: AgentContext) -> Plan:
        selected_tools = self.select_tools(context)
        support_tools = self.support_tools(context)
        pipeline = self.build_pipeline(context, selected_tools, support_tools)
        plan = Plan(
            rationale=pipeline.hypothesis,
            commands=pipeline.next_commands[: context.max_steps],
            metadata={
                "source": "specialist",
                "category": self.category,
                "selected_tools": [tool.to_dict() for tool in selected_tools],
                "support_tools": [tool.to_dict() for tool in support_tools],
                "hypothesis": pipeline.hypothesis,
                "evidence": pipeline.evidence,
                "next_commands": [command.to_dict() for command in pipeline.next_commands],
                "notes": pipeline.notes,
            },
        )
        if context.message_bus:
            context.message_bus.add_hypothesis(self.name, pipeline.hypothesis, category=self.category, evidence=pipeline.evidence)
            for evidence in pipeline.evidence:
                context.message_bus.add_observation(self.name, evidence, category=self.category)
        context.trace_store.append(
            TraceEvent(
                challenge_id=context.state.challenge.id,
                agent=self.name,
                action="specialist-triage",
                stdout=pipeline.hypothesis,
                metadata={"pipeline": pipeline.to_dict(), "plan": plan.to_dict()},
            )
        )
        return plan

    def build_pipeline(self, context: AgentContext, tools: list[ToolSpec], support_tools: list[ToolSpec]) -> TriagePipeline:
        commands: list[PlanCommand] = []
        evidence = self._base_evidence(context)
        for file_name in context.state.challenge.files:
            commands.extend(self._generic_file_triage(file_name, context.timeout, support_tools))
        if not commands:
            commands.append(_python_workspace_flag_scan(context.timeout))
        return TriagePipeline(
            category=self.category,
            hypothesis=f"{self.name} will perform safe local triage for category={self.category}.",
            evidence=evidence,
            next_commands=commands,
        )

    def _base_evidence(self, context: AgentContext) -> list[str]:
        challenge = context.state.challenge
        evidence = [f"metadata category={challenge.category}"]
        if challenge.connection:
            evidence.append(f"challenge connection={challenge.connection}")
        if challenge.description:
            evidence.append(f"description keywords={_keywords(challenge.description)}")
        if challenge.files:
            evidence.append(f"files={', '.join(challenge.files)}")
        return evidence

    def _generic_file_triage(self, file_name: str, timeout: int, support_tools: list[ToolSpec]) -> list[PlanCommand]:
        tool_names = {tool.name for tool in support_tools}
        quoted = shlex.quote(file_name)
        commands: list[PlanCommand] = []
        if "file" in tool_names:
            commands.append(_tool_command("file", f"file {quoted}", f"Identify file type for {file_name}", timeout=15, file=file_name))
        if "strings" in tool_names:
            commands.append(
                _tool_command(
                    "strings",
                    f"_t={quoted}; if command -v strings >/dev/null 2>&1; then strings -a -- \"$_t\" | head -n 240; else echo 'missing tool: strings'; fi",
                    f"Extract printable strings from {file_name}",
                    timeout=timeout,
                    file=file_name,
                )
            )
        commands.append(_python_text_scan(file_name, timeout))
        return commands


class PwnAgent(SpecialistAgent):
    def __init__(self) -> None:
        super().__init__("pwn-specialist", "Plan safe local binary exploitation triage.", "pwn")

    def build_pipeline(self, context: AgentContext, tools: list[ToolSpec], support_tools: list[ToolSpec]) -> TriagePipeline:
        names = {tool.name for tool in tools}
        commands: list[PlanCommand] = []
        evidence = self._base_evidence(context)
        evidence.extend(_file_evidence(context, {"elf": "ELF candidate", "libc": "libc-related text", "overflow": "overflow hint", "canary": "stack canary hint"}))
        for file_name in context.state.challenge.files:
            commands.extend(self._generic_file_triage(file_name, context.timeout, support_tools))
            quoted = shlex.quote(file_name)
            if "checksec" in names:
                commands.append(
                    _tool_command(
                        "checksec",
                        f"_t={quoted}; if command -v checksec >/dev/null 2>&1; then checksec --file=\"$_t\"; else echo 'missing tool: checksec'; fi",
                        f"Check exploit mitigations for {file_name}",
                        timeout=20,
                        file=file_name,
                    )
                )
            commands.append(
                _tool_command(
                    "readelf",
                    f"_t={quoted}; if command -v readelf >/dev/null 2>&1; then readelf -h -- \"$_t\" && readelf -l -- \"$_t\" | head -n 120; else echo 'missing tool: readelf'; fi",
                    f"Inspect ELF headers and program headers for {file_name}",
                    timeout=20,
                    file=file_name,
                )
            )
        commands.append(_write_text_file_command("solve.py", _pwn_solve_template(context), "Create pwntools solve.py template with local/remote parameters.", context.timeout))
        commands.append(_write_text_file_command("gdb-notes.md", _pwn_gdb_notes(context), "Create GDB debugging notes for breakpoint and mitigation review.", context.timeout))
        return TriagePipeline(
            category="pwn",
            hypothesis="Pwn triage will identify binary format, mitigations, useful strings, remote parameters, and prepare solve/debug templates.",
            evidence=evidence,
            next_commands=commands,
            notes=["Remote execution is parameterized only from challenge.connection; no automatic public attack is performed."],
        )


class RevAgent(SpecialistAgent):
    def __init__(self) -> None:
        super().__init__("rev-specialist", "Plan local reverse engineering triage.", "rev")

    def build_pipeline(self, context: AgentContext, tools: list[ToolSpec], support_tools: list[ToolSpec]) -> TriagePipeline:
        names = {tool.name for tool in tools}
        commands: list[PlanCommand] = []
        evidence = self._base_evidence(context)
        evidence.extend(_file_evidence(context, {"elf": "ELF binary", "pe": "PE/Windows binary", ".pyc": "Python bytecode", ".apk": "APK placeholder"}))
        for file_name in context.state.challenge.files:
            quoted = shlex.quote(file_name)
            commands.extend(self._generic_file_triage(file_name, context.timeout, support_tools))
            commands.append(_python_magic_classifier(file_name, context.timeout))
            commands.append(
                _tool_command(
                    "readelf",
                    f"_t={quoted}; if command -v readelf >/dev/null 2>&1; then readelf -h -- \"$_t\" && readelf -s -- \"$_t\" | head -n 120; else echo 'readelf skipped: not installed or not ELF'; fi",
                    f"Inspect ELF header and symbol table for {file_name}",
                    timeout=20,
                    file=file_name,
                )
            )
            if "objdump" in names:
                commands.append(
                    _tool_command(
                        "objdump",
                        f"_t={quoted}; if command -v objdump >/dev/null 2>&1; then objdump -d -M intel -- \"$_t\" | head -n 260; else echo 'missing tool: objdump'; fi",
                        f"Disassemble first functions in {file_name}",
                        timeout=context.timeout,
                        file=file_name,
                    )
                )
            if "radare2" in names:
                commands.append(
                    _tool_command(
                        "radare2",
                        f"_t={quoted}; if command -v r2 >/dev/null 2>&1; then r2 -q -A -c 'iI; afl~main; izz~flag' -- \"$_t\"; else echo 'missing tool: r2/radare2'; fi",
                        f"Run light radare2 metadata/functions/strings triage for {file_name}",
                        timeout=context.timeout,
                        file=file_name,
                    )
                )
        return TriagePipeline(
            category="rev",
            hypothesis="Rev triage will classify binary family, inspect strings/imports/symbols, and disassemble enough code to choose a reversing route.",
            evidence=evidence,
            next_commands=commands,
            notes=["APK, PE, and Python bytecode handling is detected and traced; heavyweight decompilation remains a future profile."],
        )


class CryptoAgent(SpecialistAgent):
    def __init__(self) -> None:
        super().__init__("crypto-specialist", "Plan local crypto challenge triage.", "crypto")

    def build_pipeline(self, context: AgentContext, tools: list[ToolSpec], support_tools: list[ToolSpec]) -> TriagePipeline:
        commands: list[PlanCommand] = []
        evidence = self._base_evidence(context)
        evidence.extend(_crypto_evidence(context))
        for file_name in context.state.challenge.files:
            commands.extend(self._generic_file_triage(file_name, context.timeout, support_tools))
            commands.append(_crypto_pattern_scan(file_name, context.timeout))
        commands.append(_write_text_file_command("solve.py", _crypto_solve_template(context), "Create crypto solve.py draft for RSA/base encodings/XOR/LCG/substitution routes.", context.timeout))
        commands.append(
            PlanCommand(
                command="python3 solve.py",
                reason="Run generated crypto draft against local challenge files.",
                timeout=context.timeout,
                metadata={"tool": "python", "strategy": "crypto-draft-run"},
            )
        )
        return TriagePipeline(
            category="crypto",
            hypothesis="Crypto triage will detect RSA, base64/hex/XOR encodings, small exponent/common modulus clues, LCG patterns, and substitution text.",
            evidence=evidence,
            next_commands=commands,
            notes=["Sage and RsaCtfTool remain optional profiles; the default draft uses Python standard library first."],
        )


class WebAgent(SpecialistAgent):
    def __init__(self) -> None:
        super().__init__("web-specialist", "Plan authorized web challenge triage.", "web")

    def build_pipeline(self, context: AgentContext, tools: list[ToolSpec], support_tools: list[ToolSpec]) -> TriagePipeline:
        commands: list[PlanCommand] = []
        evidence = self._base_evidence(context)
        connection = context.state.challenge.connection or ""
        if connection.startswith(("http://", "https://")):
            evidence.append("HTTP(S) challenge connection provided; safe curl-only triage enabled.")
            url = shlex.quote(connection.rstrip("/"))
            commands.extend(
                [
                    _tool_command("curl", f"mkdir -p ../artifacts/web && curl -kisS --max-time 10 {url} -o ../artifacts/web/index.http", "Fetch challenge URL headers and body into artifacts.", timeout=15),
                    _tool_command("curl", f"curl -kisS --max-time 10 {url}/robots.txt || true", "Check robots.txt on challenge URL.", timeout=15),
                    _tool_command(
                        "curl",
                        f"for p in admin login flag robots.txt; do echo \"### /$p\"; curl -ksS --max-time 5 -o - -w '\\nHTTP=%{{http_code}}\\n' {url}/$p | head -n 80; done",
                        "Run small bounded directory probe against common challenge paths.",
                        timeout=context.timeout,
                    ),
                    _tool_command(
                        "curl",
                        f"for q in 'id=1' 'q=test' 'debug=1'; do echo \"### ?$q\"; curl -ksS --max-time 5 {url}/?$q | head -n 80; done",
                        "Run bounded parameter fuzz with harmless values on challenge URL.",
                        timeout=context.timeout,
                    ),
                ]
            )
        for file_name in context.state.challenge.files:
            commands.extend(self._generic_file_triage(file_name, context.timeout, support_tools))
            commands.append(_web_form_scan(file_name, context.timeout))
            if Path(file_name).suffix.lower() in {".php", ".phtml", ".inc"}:
                commands.append(_php_source_analysis(file_name, context.timeout))
                commands.append(_php_lfi_local_replay(file_name, context.timeout))
        if not commands:
            commands.append(_python_workspace_flag_scan(context.timeout))
        return TriagePipeline(
            category="web",
            hypothesis="Web triage will collect headers/body, robots, bounded directory probes, harmless parameter fuzz, and local form hints.",
            evidence=evidence,
            next_commands=commands,
            notes=["Network activity is only generated for challenge.connection and remains bounded by curl max-time/path lists."],
        )


class ForensicsAgent(SpecialistAgent):
    def __init__(self) -> None:
        super().__init__("forensics-specialist", "Plan local forensics challenge triage.", "forensics")

    def build_pipeline(self, context: AgentContext, tools: list[ToolSpec], support_tools: list[ToolSpec]) -> TriagePipeline:
        names = {tool.name for tool in tools}
        commands: list[PlanCommand] = []
        evidence = self._base_evidence(context)
        evidence.extend(_file_evidence(context, {"png": "image/stego candidate", "zip": "archive/carving candidate", "pcap": "packet capture candidate", "pdf": "document metadata candidate"}))
        for file_name in context.state.challenge.files:
            commands.extend(self._generic_file_triage(file_name, context.timeout, support_tools))
            quoted = shlex.quote(file_name)
            if "binwalk" in names:
                commands.append(
                    _tool_command(
                        "binwalk",
                        f"_t={quoted}; if command -v binwalk >/dev/null 2>&1; then binwalk -- \"$_t\"; else echo 'missing tool: binwalk'; fi",
                        f"Scan embedded signatures in {file_name}",
                        timeout=context.timeout,
                        file=file_name,
                    )
                )
            if "exiftool" in names:
                commands.append(
                    _tool_command(
                        "exiftool",
                        f"_t={quoted}; if command -v exiftool >/dev/null 2>&1; then exiftool -- \"$_t\"; else echo 'missing tool: exiftool'; fi",
                        f"Extract metadata from {file_name}",
                        timeout=context.timeout,
                        file=file_name,
                    )
                )
            commands.append(
                _tool_command(
                    "hexdump",
                    f"_t={quoted}; if command -v hexdump >/dev/null 2>&1; then hexdump -C -- \"$_t\" | head -n 120; elif command -v xxd >/dev/null 2>&1; then xxd -g1 -l 2048 -- \"$_t\"; else echo 'missing tool: hexdump/xxd'; fi",
                    f"Inspect leading bytes and embedded readable data in {file_name}",
                    timeout=20,
                    file=file_name,
                )
            )
            commands.append(_forensics_carve_command(file_name, context.timeout))
        return TriagePipeline(
            category="forensics",
            hypothesis="Forensics triage will identify file type, metadata, strings, hex signatures, and export carved files into artifacts.",
            evidence=evidence,
            next_commands=commands,
            notes=["Carved files are written under ../artifacts/forensics so they survive reporting and UI browsing."],
        )


def specialist_for_category(category: str) -> SpecialistAgent:
    return {
        "pwn": PwnAgent,
        "web": WebAgent,
        "crypto": CryptoAgent,
        "rev": RevAgent,
        "forensics": ForensicsAgent,
    }.get(category, SpecialistAgentFactory)()


class SpecialistAgentFactory(SpecialistAgent):
    def __init__(self) -> None:
        super().__init__("generic-specialist", "Plan generic local CTF triage.", "generic")


def _tool_command(tool: str, command: str, reason: str, *, timeout: int, file: str | None = None) -> PlanCommand:
    return PlanCommand(command=command, reason=reason, timeout=timeout, metadata={"tool": tool, "file": file, "pipeline": "specialist-triage"})


def _python_text_scan(file_name: str, timeout: int) -> PlanCommand:
    code = (
        "from pathlib import Path; "
        f"p=Path({file_name!r}); "
        "data=p.read_bytes(); "
        "print(data[:200000].decode('utf-8','replace'))"
    )
    return PlanCommand(
        command="python3 -c " + shlex.quote(code),
        reason=f"Read printable content from {file_name} using Python fallback.",
        timeout=timeout,
        metadata={"file": file_name, "tool": "python", "strategy": "specialist-python-text-scan", "pipeline": "specialist-triage"},
    )


def _python_workspace_flag_scan(timeout: int) -> PlanCommand:
    code = (
        "from pathlib import Path; "
        "[print('\\n###', p, '\\n' + p.read_bytes()[:200000].decode('utf-8','replace')) "
        "for p in Path('.').rglob('*') if p.is_file()]"
    )
    return PlanCommand(command="python3 -c " + shlex.quote(code), reason="Scan all workspace files with Python fallback.", timeout=timeout, metadata={"tool": "python", "strategy": "workspace-text-scan"})


def _write_text_file_command(path: str, content: str, reason: str, timeout: int) -> PlanCommand:
    code = f"from pathlib import Path; Path({path!r}).write_text({content!r}, encoding='utf-8'); print('wrote {path}')"
    return PlanCommand(command="python3 -c " + shlex.quote(code), reason=reason, timeout=min(timeout, 15), metadata={"tool": "python", "artifact": path, "pipeline": "specialist-triage"})


def _python_magic_classifier(file_name: str, timeout: int) -> PlanCommand:
    code = (
        "from pathlib import Path; "
        f"p=Path({file_name!r}); b=p.read_bytes()[:16]; s=p.suffix.lower(); "
        "kind='unknown'; "
        "kind='ELF' if b.startswith(b'\\x7fELF') else kind; "
        "kind='PE' if b.startswith(b'MZ') else kind; "
        "kind='Python bytecode' if b[:4] in {b'\\xa7\\r\\r\\n', b'\\xcb\\r\\r\\n'} or s=='.pyc' else kind; "
        "kind='APK/ZIP' if b.startswith(b'PK\\x03\\x04') and s=='.apk' else kind; "
        "print(f'{p}: {kind} magic={b.hex()} suffix={s}')"
    )
    return PlanCommand(command="python3 -c " + shlex.quote(code), reason=f"Classify executable family for {file_name}", timeout=min(timeout, 15), metadata={"tool": "python", "file": file_name, "strategy": "magic-classifier"})


def _crypto_pattern_scan(file_name: str, timeout: int) -> PlanCommand:
    code = r"""
import base64, binascii, re
from pathlib import Path
p=Path(FILE)
text=p.read_bytes()[:300000].decode('utf-8','replace')
print('crypto-patterns for', p)
patterns={
 'rsa_modulus': r'\b[necpq]\s*=\s*[0-9]{4,}',
 'rsa_hex': r'0x[0-9a-fA-F]{64,}',
 'base64': r'\b[A-Za-z0-9+/]{20,}={0,2}\b',
 'hex': r'\b[0-9a-fA-F]{32,}\b',
 'xor': r'xor|key|repeating',
 'small_exponent': r'\be\s*=\s*(3|5|17)\b',
 'common_modulus': r'common modulus|same n|shared modulus',
 'lcg': r'lcg|linear congruential|modulus|multiplier|increment',
 'substitution': r'substitution|frequency|alphabet',
}
for name, pattern in patterns.items():
    hits=re.findall(pattern, text, flags=re.I)
    if hits:
        print(name, 'hits=', len(hits), 'sample=', hits[:3])
for token in re.findall(patterns['base64'], text):
    try:
        decoded=base64.b64decode(token, validate=True)
    except Exception:
        continue
    if decoded:
        print('base64-decoded-sample', decoded[:120].decode('utf-8','replace'))
for token in re.findall(patterns['hex'], text):
    try:
        decoded=binascii.unhexlify(token)
    except Exception:
        continue
    if decoded:
        print('hex-decoded-sample', decoded[:120].decode('utf-8','replace'))
""".strip().replace("FILE", repr(file_name))
    return PlanCommand(command="python3 -c " + shlex.quote(code), reason=f"Detect crypto primitives and easy encodings in {file_name}", timeout=timeout, metadata={"tool": "python", "file": file_name, "strategy": "crypto-pattern-scan"})


def _web_form_scan(file_name: str, timeout: int) -> PlanCommand:
    code = f"""
from html.parser import HTMLParser
from pathlib import Path

class P(HTMLParser):
    def handle_starttag(self, tag, attrs):
        if tag in {{'form', 'input', 'button', 'textarea', 'select'}}:
            print(tag, dict(attrs))

P().feed(Path({file_name!r}).read_bytes()[:300000].decode('utf-8', 'replace'))
""".strip()
    return PlanCommand(command="python3 -c " + shlex.quote(code), reason=f"Identify forms and inputs in {file_name}", timeout=min(timeout, 15), metadata={"tool": "python", "file": file_name, "strategy": "web-form-scan"})


def _php_source_analysis(file_name: str, timeout: int) -> PlanCommand:
    code = (
        "from pathlib import Path; import json, sys; "
        f"sys.path.insert(0, {_PROJECT_SRC!r}); "
        "from ctf_agent.analysis.php import analyze_php_text; "
        f"p=Path({file_name!r}); "
        "analysis=analyze_php_text(p.read_text(encoding='utf-8', errors='replace')).to_dict(); "
        "out=Path('../artifacts/web') / (p.name + '.php-analysis.json'); "
        "out.parent.mkdir(parents=True, exist_ok=True); "
        "out.write_text(json.dumps(analysis, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8'); "
        "print('php-analysis-json', out); print(json.dumps(analysis, ensure_ascii=False, sort_keys=True))"
    )
    return PlanCommand(
        command="python3 -c " + shlex.quote(code),
        reason=f"Analyze PHP source for parameters, loose comparisons, include sinks, blacklists, and wrapper/type-juggling strategies in {file_name}.",
        timeout=min(timeout, 20),
        metadata={"tool": "python", "file": file_name, "strategy": "php-source-analysis", "pipeline": "specialist-triage"},
    )


def _php_lfi_local_replay(file_name: str, timeout: int) -> PlanCommand:
    code = (
        "from pathlib import Path; import json, sys; "
        f"sys.path.insert(0, {_PROJECT_SRC!r}); "
        "from ctf_agent.analysis.php import lfi_replay_candidates; "
        f"p=Path({file_name!r}); "
        "candidates=lfi_replay_candidates(p, Path('.')); "
        "print('php-lfi-local-replay', json.dumps(candidates, ensure_ascii=False, sort_keys=True)); "
        "[print(item.get('simulated_output','')) for item in candidates if item.get('simulated_output')]"
    )
    return PlanCommand(
        command="python3 -c " + shlex.quote(code),
        reason=f"Use bounded local replay to test PHP include candidates against sibling PHP files for {file_name}.",
        timeout=min(timeout, 20),
        metadata={"tool": "python", "file": file_name, "strategy": "php-lfi-local-replay", "pipeline": "specialist-triage"},
    )


def _forensics_carve_command(file_name: str, timeout: int) -> PlanCommand:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(file_name).name)
    quoted = shlex.quote(file_name)
    command = (
        f"mkdir -p ../artifacts/forensics/{safe_name} && "
        f"_t={quoted}; "
        "if command -v binwalk >/dev/null 2>&1; then binwalk -e --run-as=$(id -un) --directory ../artifacts/forensics/"
        f"{safe_name} -- \"$_t\" || true; "
        "elif command -v foremost >/dev/null 2>&1; then foremost -i \"$_t\" -o ../artifacts/forensics/"
        f"{safe_name} || true; "
        "else python3 - <<'PY'\n"
        "from pathlib import Path\n"
        f"src=Path({file_name!r})\n"
        f"out=Path('../artifacts/forensics/{safe_name}/carved-copy.bin')\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        "out.write_bytes(src.read_bytes())\n"
        "print('carved fallback copy', out)\n"
        "PY\n"
        "fi"
    )
    return PlanCommand(command=command, reason=f"Export carved/fallback files for {file_name}", timeout=timeout, metadata={"tool": "binwalk/foremost/python", "file": file_name, "artifact_dir": f"../artifacts/forensics/{safe_name}", "strategy": "forensics-carve"})


def _pwn_solve_template(context: AgentContext) -> str:
    host, port = _parse_host_port(context.state.challenge.connection)
    binary = context.state.challenge.files[0] if context.state.challenge.files else "./chall"
    return f"""#!/usr/bin/env python3
from pwn import *

BINARY = {binary!r}
HOST = {host!r}
PORT = {port!r}

context.binary = ELF(BINARY, checksec=False)

def start():
    if args.REMOTE and HOST and PORT:
        return remote(HOST, int(PORT))
    return process(BINARY)

def main():
    io = start()
    # TODO: inspect checksec/readelf/strings results, then build payload.
    # Example skeleton:
    # payload = flat({{offset: p64(context.binary.symbols.get('win', 0))}})
    # io.sendlineafter(b'> ', payload)
    io.interactive()

if __name__ == "__main__":
    main()
"""


def _pwn_gdb_notes(context: AgentContext) -> str:
    binary = context.state.challenge.files[0] if context.state.challenge.files else "./chall"
    host, port = _parse_host_port(context.state.challenge.connection)
    return f"""# GDB Notes

- Binary: `{binary}`
- Remote host: `{host or ""}`
- Remote port: `{port or ""}`

Suggested local session:

```bash
gdb -q {shlex.quote(binary)}
checksec --file={shlex.quote(binary)}
readelf -h {shlex.quote(binary)}
```

Breakpoints to consider: `main`, input parser, `win`, `system`, and crash location after cyclic pattern.
"""


def _crypto_solve_template(context: AgentContext) -> str:
    files = context.state.challenge.files
    return f"""#!/usr/bin/env python3
import base64
import binascii
import math
import re
from pathlib import Path

FILES = {files!r}
FLAG_RE = re.compile(rb"flag\\{{[^}}]+\\}}")

def try_decoders(data: bytes):
    yield data
    for token in re.findall(rb"[A-Za-z0-9+/]{{20,}}={{0,2}}", data):
        try:
            yield base64.b64decode(token, validate=True)
        except Exception:
            pass
    for token in re.findall(rb"[0-9a-fA-F]{{32,}}", data):
        try:
            yield binascii.unhexlify(token)
        except Exception:
            pass

def parse_ints(text: str):
    return {{name: int(value, 0) for name, value in re.findall(r"\\b([ncepq])\\s*=\\s*(0x[0-9a-fA-F]+|[0-9]+)", text)}}

def rsa_notes(values):
    if "n" in values and values.get("e") in (3, 5, 17):
        print("small exponent candidate", values["e"])
    if "p" in values and "q" in values:
        print("p/q provided; try phi=(p-1)*(q-1), d=inverse(e, phi)")
    if "n" in values and "c" in values:
        print("RSA tuple present; try RsaCtfTool or custom math route")

def main():
    for name in FILES:
        data = Path(name).read_bytes()
        text = data.decode("utf-8", "replace")
        print("###", name)
        values = parse_ints(text)
        if values:
            print("ints", values)
            rsa_notes(values)
        if re.search(r"lcg|linear congruential|multiplier|increment", text, re.I):
            print("LCG candidate: recover modulus/multiplier/increment from outputs")
        if re.search(r"substitution|frequency|alphabet", text, re.I):
            print("substitution candidate: try frequency analysis/known plaintext")
        for decoded in try_decoders(data):
            m = FLAG_RE.search(decoded)
            if m:
                print(m.group(0).decode())

if __name__ == "__main__":
    main()
"""


def _parse_host_port(connection: str | None) -> tuple[str | None, str | None]:
    if not connection:
        return None, None
    text = connection.strip()
    if text.startswith("nc "):
        parts = text.split()
        if len(parts) >= 3:
            return parts[1], parts[2]
    parsed = urlparse(text if "://" in text else "//" + text)
    if parsed.hostname and parsed.port:
        return parsed.hostname, str(parsed.port)
    pieces = text.rsplit(":", 1)
    if len(pieces) == 2 and pieces[1].isdigit():
        return pieces[0], pieces[1]
    return None, None


def _keywords(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9_+.-]{3,}", text.lower())
    return ", ".join(words[:12])


def _file_evidence(context: AgentContext, patterns: dict[str, str]) -> list[str]:
    evidence: list[str] = []
    text = " ".join([context.state.challenge.title, context.state.challenge.description, " ".join(context.state.challenge.files)]).lower()
    for pattern, note in patterns.items():
        if pattern.lower() in text:
            evidence.append(note)
    for file_name in context.state.challenge.files:
        suffix = Path(file_name).suffix.lower()
        if suffix == ".exe":
            evidence.append(f"{file_name}: PE extension")
        elif suffix == ".apk":
            evidence.append(f"{file_name}: APK extension")
        elif suffix == ".pyc":
            evidence.append(f"{file_name}: Python bytecode extension")
    return evidence


def _crypto_evidence(context: AgentContext) -> list[str]:
    text = " ".join([context.state.challenge.title, context.state.challenge.description, " ".join(context.state.challenge.hints), " ".join(context.state.challenge.files)]).lower()
    mapping = {
        "rsa": "RSA keyword",
        "base64": "base64 keyword",
        "hex": "hex keyword",
        "xor": "XOR keyword",
        "small exponent": "small exponent clue",
        "common modulus": "common modulus clue",
        "lcg": "LCG clue",
        "substitution": "substitution clue",
    }
    return [note for key, note in mapping.items() if key in text]
