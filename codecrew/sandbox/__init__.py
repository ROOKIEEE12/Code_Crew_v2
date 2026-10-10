"""Sandbox backends for executing generated code."""

from __future__ import annotations

from pathlib import Path

from .base import ExecResult, Sandbox, SandboxError
from .docker import DockerSandbox, docker_available
from .local import LocalSandbox

__all__ = [
    "ExecResult",
    "Sandbox",
    "SandboxError",
    "DockerSandbox",
    "LocalSandbox",
    "docker_available",
    "get_sandbox",
]


def get_sandbox(workspace: Path, prefer: str = "auto") -> Sandbox:
    """Pick a backend.

    ``auto`` prefers the container backend and falls back to the local venv,
    so the pipeline runs anywhere while still using real isolation when it
    is available.
    """
    if prefer == "local":
        return LocalSandbox(workspace)
    if prefer == "docker":
        if not docker_available():
            raise SandboxError("docker requested but not available")
        return DockerSandbox(workspace)
    if prefer != "auto":
        raise SandboxError(f"unknown sandbox backend: {prefer!r}")
    if docker_available():
        return DockerSandbox(workspace)
    return LocalSandbox(workspace)
