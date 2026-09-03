from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ctf_agent.core.config import project_root
from ctf_agent.sandbox.executor import docker_available


@dataclass(frozen=True)
class DockerProfile:
    name: str
    image: str
    dockerfile: str
    description: str
    core_checks: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "image": self.image,
            "dockerfile": self.dockerfile,
            "description": self.description,
            "core_checks": [list(check) for check in self.core_checks],
            "notes": list(self.notes),
        }


DOCKER_PROFILES: dict[str, DockerProfile] = {
    "generic": DockerProfile(
        name="generic",
        image="ctf-agent:generic",
        dockerfile="docker/Dockerfile.generic",
        description="Small general triage image for local file and text inspection.",
        core_checks=[
            ["python3", "--version"],
            ["file", "--version"],
            ["readelf", "--version"],
            ["curl", "--version"],
            ["rg", "--version"],
            ["jq", "--version"],
            ["sh", "-lc", "command -v xxd"],
            ["nc", "-h"],
        ],
    ),
    "pwn": DockerProfile(
        name="pwn",
        image="ctf-agent:pwn",
        dockerfile="docker/Dockerfile.pwn",
        description="Binary exploitation triage image.",
        core_checks=[
            ["gdb", "--version"],
            ["gdbserver", "--version"],
            ["checksec", "--help"],
            ["python3", "-c", "import pwn; print('pwntools ok')"],
            ["ROPgadget", "--help"],
        ],
        notes=["one_gadget is documented as optional in /opt/ctf-agent/README.pwn-tools because it is Ruby/gem dependent and challenge-specific."],
    ),
    "web": DockerProfile(
        name="web",
        image="ctf-agent:web",
        dockerfile="docker/Dockerfile.web",
        description="Authorized web challenge probing image.",
        core_checks=[
            ["curl", "--version"],
            ["nmap", "--version"],
            ["ffuf", "-V"],
            ["sqlmap", "--version"],
            ["python3", "-c", "import requests, httpx; print('http clients ok')"],
        ],
    ),
    "crypto": DockerProfile(
        name="crypto",
        image="ctf-agent:crypto",
        dockerfile="docker/Dockerfile.crypto",
        description="Python-first crypto and math image.",
        core_checks=[
            ["python3", "-c", "import sympy, Cryptodome, z3; print(\"crypto libs ok\")"],
        ],
        notes=["Sage is intentionally split into the optional sage profile/image because it is large."],
    ),
    "rev": DockerProfile(
        name="rev",
        image="ctf-agent:rev",
        dockerfile="docker/Dockerfile.rev",
        description="Reverse engineering triage image.",
        core_checks=[
            ["readelf", "--version"],
            ["objdump", "--version"],
            ["strings", "--version"],
            ["gdb", "--version"],
            ["strace", "-V"],
            ["ltrace", "-V"],
            ["sh", "-lc", "command -v rizin || command -v radare2"],
        ],
    ),
    "forensics": DockerProfile(
        name="forensics",
        image="ctf-agent:forensics",
        dockerfile="docker/Dockerfile.forensics",
        description="Forensics and stego triage image.",
        core_checks=[
            ["binwalk", "--help"],
            ["exiftool", "-ver"],
            ["foremost", "-V"],
            ["sh", "-lc", "command -v pngcheck && pngcheck -h >/tmp/pngcheck-help 2>&1"],
            ["steghide", "--version"],
        ],
        notes=["zsteg installation notes are included in the image at /opt/ctf-agent/README.zsteg."],
    ),
    "sage": DockerProfile(
        name="sage",
        image="ctf-agent:sage",
        dockerfile="",
        description="Optional external SageMath profile; configure to sagemath/sagemath when needed.",
        core_checks=[["sage", "--version"]],
        notes=["No local Dockerfile is provided because Sage should stay separate from the smaller crypto image."],
    ),
}


BUILDABLE_PROFILES = tuple(name for name, profile in DOCKER_PROFILES.items() if profile.dockerfile)


def profile_names(include_optional: bool = False) -> list[str]:
    names = list(BUILDABLE_PROFILES)
    if include_optional:
        names.extend(name for name in DOCKER_PROFILES if name not in names)
    return names


def get_profile(name: str) -> DockerProfile:
    try:
        return DOCKER_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown Docker profile: {name}") from exc


def dockerfile_path(profile: DockerProfile, root: str | Path | None = None) -> Path:
    if not profile.dockerfile:
        raise ValueError(f"Profile {profile.name} does not have a local Dockerfile")
    base = Path(root).expanduser() if root else project_root()
    return (base / profile.dockerfile).resolve()


def image_exists(image: str) -> bool:
    if not docker_available():
        return False
    completed = subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return completed.returncode == 0


def build_profile(profile_name: str, *, root: str | Path | None = None, no_cache: bool = False, pull: bool = False) -> dict[str, Any]:
    profile = get_profile(profile_name)
    dockerfile = dockerfile_path(profile, root=root)
    base = Path(root).expanduser() if root else project_root()
    cmd = ["docker", "build", "-f", str(dockerfile), "-t", profile.image]
    if pull:
        cmd.append("--pull")
    if no_cache:
        cmd.append("--no-cache")
    cmd.append(str(base))
    completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return {
        "profile": profile.name,
        "image": profile.image,
        "dockerfile": str(dockerfile),
        "command": cmd,
        "ok": completed.returncode == 0,
        "exit_code": completed.returncode,
        "output": completed.stdout[-8000:],
    }


def check_profile(profile_name: str, *, run_tools: bool = False) -> dict[str, Any]:
    profile = get_profile(profile_name)
    exists = image_exists(profile.image)
    checks = []
    if exists and run_tools:
        for check in profile.core_checks:
            cmd = ["docker", "run", "--rm", "--network", "none", profile.image, *check]
            completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, check=False)
            checks.append(
                {
                    "command": check,
                    "ok": completed.returncode == 0,
                    "exit_code": completed.returncode,
                    "output": "\n".join(completed.stdout.splitlines()[:5]),
                }
            )
    return {
        "profile": profile.name,
        "image": profile.image,
        "dockerfile": profile.dockerfile,
        "build_command": f"ctf-agent docker build --profile {profile.name}" if profile.dockerfile else None,
        "exists": exists,
        "ok": exists and all(check["ok"] for check in checks),
        "checks": checks,
        "notes": list(profile.notes),
    }


def docker_profiles_doctor(*, run_tools: bool = False, include_optional: bool = False) -> dict[str, Any]:
    available = docker_available()
    profiles = []
    for name in profile_names(include_optional=include_optional):
        if available:
            profiles.append(check_profile(name, run_tools=run_tools))
        else:
            profile = get_profile(name)
            profiles.append(
                {
                    "profile": profile.name,
                    "image": profile.image,
                    "dockerfile": profile.dockerfile,
                    "build_command": f"ctf-agent docker build --profile {profile.name}" if profile.dockerfile else None,
                    "exists": False,
                    "ok": False,
                    "checks": [],
                    "notes": list(profile.notes),
                }
            )
    return {
        "docker_available": available,
        "run_tools": run_tools,
        "profiles": profiles,
        "ok": bool(available and all(profile["ok"] for profile in profiles if profile["dockerfile"])),
    }
