"""Sandbox backends and execution policies."""

from ctf_agent.sandbox.docker import DockerExecutor, image_for_category
from ctf_agent.sandbox.executor import (
    CommandSafetyError,
    ExecutionResult,
    Executor,
    LocalExecutor,
    WorkspaceBoundaryError,
    docker_available,
)
from ctf_agent.sandbox.images import (
    BUILDABLE_PROFILES,
    DOCKER_PROFILES,
    DockerProfile,
    build_profile,
    check_profile,
    docker_profiles_doctor,
    get_profile,
    profile_names,
)

__all__ = [
    "CommandSafetyError",
    "DockerExecutor",
    "ExecutionResult",
    "Executor",
    "LocalExecutor",
    "WorkspaceBoundaryError",
    "BUILDABLE_PROFILES",
    "DOCKER_PROFILES",
    "docker_available",
    "DockerProfile",
    "build_profile",
    "check_profile",
    "docker_profiles_doctor",
    "get_profile",
    "image_for_category",
    "profile_names",
]
