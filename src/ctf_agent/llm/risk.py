from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    REFUSE = "refuse"


@dataclass(frozen=True)
class CommandRisk:
    level: RiskLevel
    reason: str
    confirm_required: bool = False
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level.value,
            "reason": self.reason,
            "confirm_required": self.confirm_required,
            "metadata": dict(self.metadata),
        }


_REFUSED_COMMANDS = {
    "sudo",
    "su",
    "doas",
    "passwd",
    "useradd",
    "usermod",
    "mkfs",
    "mkswap",
    "mount",
    "umount",
    "reboot",
    "shutdown",
    "poweroff",
    "iptables",
    "nft",
}
_HIGH_RISK_COMMANDS = {"rm", "rmdir", "unlink", "shred", "truncate", "dd", "mv", "chmod", "chown"}
_NETWORK_COMMANDS = {"curl", "wget", "nc", "netcat", "nmap", "ffuf", "sqlmap", "nikto", "gobuster", "feroxbuster"}
_LOW_RISK_COMMANDS = {
    "file",
    "strings",
    "xxd",
    "hexdump",
    "rg",
    "grep",
    "cat",
    "head",
    "tail",
    "readelf",
    "objdump",
    "python",
    "python3",
    "jq",
    "ls",
    "find",
    "stat",
}


def classify_command_risk(command: str, challenge_connection: str | None = None) -> CommandRisk:
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return CommandRisk(RiskLevel.REFUSE, f"Cannot parse command safely: {exc}")
    if not tokens:
        return CommandRisk(RiskLevel.REFUSE, "Empty command")

    lowered = command.lower()
    if re.search(r":\s*\(\s*\)\s*\{", command) or "/dev/sd" in lowered or "/dev/nvme" in lowered:
        return CommandRisk(RiskLevel.REFUSE, "Refusing shell/system destructive pattern")
    if re.search(r"(curl|wget)\b.*\|\s*(sh|bash|python|python3)\b", lowered):
        return CommandRisk(RiskLevel.REFUSE, "Refusing downloaded code execution pipeline")
    if re.search(r"rm\s+-[^\n;]*[rf][^\n;]*\s+/(\s|$)", lowered):
        return CommandRisk(RiskLevel.REFUSE, "Refusing recursive delete of filesystem root")

    command_names = [_command_name(token) for token in tokens if _looks_like_command_token(token)]
    first = command_names[0] if command_names else _command_name(tokens[0])
    if any(name in _REFUSED_COMMANDS for name in command_names):
        return CommandRisk(RiskLevel.REFUSE, "Command requires privileged or system-level operation")
    if any(name in _HIGH_RISK_COMMANDS for name in command_names):
        return CommandRisk(RiskLevel.HIGH, "Command can modify or destroy files; manual confirmation required", confirm_required=True)
    network_tools = [name for name in command_names if name in _NETWORK_COMMANDS]
    if network_tools:
        if not challenge_connection:
            return CommandRisk(
                RiskLevel.HIGH,
                "Network-capable command requested without a challenge connection",
                confirm_required=True,
                metadata={"tools": ",".join(network_tools)},
            )
        if not _command_mentions_connection(command, challenge_connection):
            return CommandRisk(
                RiskLevel.HIGH,
                "Network command does not appear constrained to the challenge connection",
                confirm_required=True,
                metadata={"connection": challenge_connection},
            )
        return CommandRisk(RiskLevel.MEDIUM, "Network command appears scoped to challenge connection", metadata={"connection": challenge_connection})
    if first in _LOW_RISK_COMMANDS:
        return CommandRisk(RiskLevel.LOW, "Local inspection command")
    return CommandRisk(RiskLevel.MEDIUM, "Unrecognized command; allowed through sandbox with normal limits")


def _command_name(token: str) -> str:
    return Path(token).name.lower()


def _looks_like_command_token(token: str) -> bool:
    if token in {"&&", "||", ";", "|"}:
        return False
    if token.startswith("-") or "=" in token:
        return False
    return True


def _command_mentions_connection(command: str, connection: str) -> bool:
    values = {connection}
    parsed = urlparse(connection if "://" in connection else "//" + connection)
    if parsed.hostname:
        values.add(parsed.hostname)
    if parsed.port:
        values.add(str(parsed.port))
    return any(value and value in command for value in values)
