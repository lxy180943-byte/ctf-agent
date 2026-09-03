"""Challenge platform adapters."""

from ctf_agent.platforms.base import PlatformAdapter, SubmissionResult
from ctf_agent.platforms.ctfd import CTFdPlatformAdapter
from ctf_agent.platforms.local import LocalPlatformAdapter

__all__ = [
    "CTFdPlatformAdapter",
    "LocalPlatformAdapter",
    "PlatformAdapter",
    "SubmissionResult",
]
