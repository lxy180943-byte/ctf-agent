from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from ctf_agent.core.config import get_nested

REQUIRED_TOOLS = ("python3", "pip", "git", "docker", "make")
OPTIONAL_TOOLS = ("python", "pip3", "uv", "rg", "gh")
DEFAULT_DIRS = ("ctf-agent", "ctf-workspace", "ctf-artifacts")

TOOL_RECOMMENDATIONS = {
    "python": "Optional alias. Use python3, activate .venv, or install: sudo apt install python-is-python3",
    "rg": "Recommended for fast source and artifact search. Install: sudo apt install ripgrep",
    "gh": "Optional GitHub workflow helper. Install: sudo apt install gh, or see https://cli.github.com/",
    "uv": "Optional faster Python package manager. Install only if desired: curl -LsSf https://astral.sh/uv/install.sh | sh",
}


@dataclass
class ToolStatus:
    name: str
    path: str | None
    ok: bool
    version: str | None = None
    error: str | None = None
    recommendation: str | None = None


@dataclass
class DirStatus:
    path: str
    exists: bool
    under_linux_home: bool


@dataclass
class LLMStatus:
    provider: str
    model: str | None
    base_url: str | None
    timeout_seconds: int
    api_key_present: bool
    ok: bool
    error: str | None = None
    recommendation: str | None = None


def run_command(cmd: Sequence[str], timeout: int = 10) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            list(cmd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # pragma: no cover
        return 127, str(exc)
    return completed.returncode, completed.stdout.strip()


def is_wsl() -> bool:
    if os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        version = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False
    return "microsoft" in version or "wsl" in version


def command_version_args(name: str) -> list[str]:
    if name == "docker_compose":
        return ["docker", "compose", "version"]
    return [name, "--version"]


def check_tool(name: str) -> ToolStatus:
    if name == "docker_compose":
        docker_path = shutil.which("docker")
        if not docker_path:
            return ToolStatus(name=name, path=None, ok=False, error="docker CLI missing")
        code, output = run_command(command_version_args(name))
        return ToolStatus(
            name=name,
            path=docker_path,
            ok=code == 0,
            version=output.splitlines()[0] if output else None,
            error=None if code == 0 else output,
            recommendation=TOOL_RECOMMENDATIONS.get(name) if code != 0 else None,
        )

    path = shutil.which(name)
    if not path:
        return ToolStatus(name=name, path=None, ok=False, error="not found in PATH", recommendation=TOOL_RECOMMENDATIONS.get(name))
    code, output = run_command(command_version_args(name))
    return ToolStatus(
        name=name,
        path=path,
        ok=code == 0,
        version=output.splitlines()[0] if output else None,
        error=None if code == 0 else output,
        recommendation=TOOL_RECOMMENDATIONS.get(name) if code != 0 else None,
    )


def check_dirs(home: Path, create: bool) -> list[DirStatus]:
    statuses: list[DirStatus] = []
    for dirname in DEFAULT_DIRS:
        path = home / dirname
        if create:
            path.mkdir(parents=True, exist_ok=True)
            if dirname == "ctf-agent":
                (path / "docs").mkdir(parents=True, exist_ok=True)
        statuses.append(
            DirStatus(
                path=str(path),
                exists=path.is_dir(),
                under_linux_home=str(path).startswith(str(home)) and not str(path).startswith("/mnt/"),
            )
        )
    return statuses


def docker_smoke(enabled: bool) -> dict[str, object]:
    if not enabled:
        return {"attempted": False, "ok": None, "output": "skipped"}
    code, output = run_command(["docker", "run", "--rm", "hello-world"], timeout=60)
    return {"attempted": True, "ok": code == 0, "output": "\n".join(output.splitlines()[:8])}


def check_llm_config(config: dict[str, Any] | None = None, environ: dict[str, str] | None = None) -> LLMStatus:
    config = config or {}
    environ = environ or os.environ
    provider = str(environ.get("CTF_AGENT_LLM_PROVIDER") or get_nested(config, ("llm", "provider")) or "none").lower()
    if provider == "openai_compatible":
        provider = "openai-compatible"
    base_url = environ.get("OPENAI_BASE_URL") or ("https://api.openai.com/v1" if provider == "openai" else None)
    model = environ.get("OPENAI_MODEL")
    api_key = environ.get("OPENAI_API_KEY")
    timeout = int(environ.get("CTF_AGENT_LLM_TIMEOUT") or get_nested(config, ("llm", "timeout_seconds")) or 60)
    if provider in {"none", "disabled", "off", "dry-run", "fallback"}:
        return LLMStatus(
            provider=provider,
            model=str(model) if model else None,
            base_url=str(base_url) if base_url else None,
            timeout_seconds=timeout,
            api_key_present=bool(api_key),
            ok=True,
            recommendation="LLM disabled; set CTF_AGENT_LLM_PROVIDER=openai and OPENAI_API_KEY/OPENAI_MODEL to enable GPT API use.",
        )
    missing = []
    unknown_provider = provider not in {"dummy", "openai", "openai-compatible"}
    if provider in {"openai", "openai-compatible"}:
        if not base_url:
            missing.append("OPENAI_BASE_URL")
        if not model:
            missing.append("OPENAI_MODEL")
        if not api_key:
            missing.append("OPENAI_API_KEY")
    ok = not missing and not unknown_provider
    return LLMStatus(
        provider=provider,
        model=str(model) if model else None,
        base_url=str(base_url) if base_url else None,
        timeout_seconds=timeout,
        api_key_present=bool(api_key),
        ok=ok,
        error=(f"missing {', '.join(missing)}" if missing else f"unknown provider {provider}" if unknown_provider else None),
        recommendation="Set OPENAI_API_KEY, OPENAI_MODEL, and OPENAI_BASE_URL for OpenAI-compatible providers." if missing else None,
    )


def build_llm_report(config: dict[str, Any] | None = None, environ: dict[str, str] | None = None) -> dict[str, object]:
    status = check_llm_config(config, environ=environ)
    return {"llm": asdict(status), "ok": status.ok}


def build_report(create_dirs: bool, docker_run: bool, config: dict[str, Any] | None = None) -> dict[str, object]:
    home = Path.home()
    tools = [check_tool(name) for name in REQUIRED_TOOLS + OPTIONAL_TOOLS + ("docker_compose",)]
    dirs = check_dirs(home, create=create_dirs)
    required_ok = all(tool.ok for tool in tools if tool.name in REQUIRED_TOOLS or tool.name == "docker_compose")
    dirs_ok = all(item.exists and item.under_linux_home for item in dirs)
    docker = docker_smoke(docker_run)
    docker_ok = docker["ok"] is not False
    return {
        "system": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "home": str(home),
            "cwd": str(Path.cwd()),
            "is_linux": platform.system().lower() == "linux",
            "is_wsl": is_wsl(),
            "wsl_distro": os.environ.get("WSL_DISTRO_NAME"),
        },
        "tools": [asdict(tool) for tool in tools],
        "dirs": [asdict(item) for item in dirs],
        "docker": docker,
        "llm": asdict(check_llm_config(config)),
        "ok": bool(required_ok and dirs_ok and docker_ok and is_wsl()),
    }


def print_llm_text(report: dict[str, object]) -> None:
    print("CTF Agent LLM Doctor")
    print(f"OK: {report['ok']}")
    llm = report["llm"]
    assert isinstance(llm, dict)
    print(f"- provider={llm['provider']} model={llm['model']} base_url={llm['base_url']} timeout={llm['timeout_seconds']}s api_key_present={llm['api_key_present']} ok={llm['ok']}")
    if llm.get("error"):
        print(f"  error: {llm['error']}")
    if llm.get("recommendation"):
        print(f"  suggestion: {llm['recommendation']}")


def print_text(report: dict[str, object]) -> None:
    print("CTF Agent Environment Doctor")
    print(f"OK: {report['ok']}")
    system = report["system"]
    assert isinstance(system, dict)
    print(f"System: linux={system['is_linux']} wsl={system['is_wsl']} distro={system['wsl_distro']} home={system['home']}")
    print("\nTools:")
    for tool in report["tools"]:
        status = "ok" if tool["ok"] else "missing/unusable"
        detail = tool.get("version") or tool.get("error") or ""
        print(f"- {tool['name']}: {status} path={tool.get('path')} {detail}")
        if tool.get("recommendation"):
            print(f"  suggestion: {tool['recommendation']}")
    print("\nDirectories:")
    for item in report["dirs"]:
        print(f"- {item['path']}: exists={item['exists']} linux_home={item['under_linux_home']}")
    docker = report["docker"]
    assert isinstance(docker, dict)
    print("\nDocker smoke:")
    print(f"- attempted={docker['attempted']} ok={docker['ok']}")
    print(str(docker["output"]))
    llm = report["llm"]
    assert isinstance(llm, dict)
    print("\nLLM:")
    print(f"- provider={llm['provider']} model={llm['model']} base_url={llm['base_url']} timeout={llm['timeout_seconds']}s api_key_present={llm['api_key_present']} ok={llm['ok']}")
    if llm.get("error"):
        print(f"  error: {llm['error']}")
    if llm.get("recommendation"):
        print(f"  suggestion: {llm['recommendation']}")
