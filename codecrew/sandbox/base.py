"""Sandbox contract.

Everything the gate layer executes is LLM-generated code, so execution always
goes through a Sandbox. There are two backends with honestly different
guarantees, and ``isolation_notes`` is what the final report quotes so the
user is never told "sandboxed" when it only means "subprocess with a timeout".
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ExecResult:
    """Outcome of one command run inside a sandbox."""

    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def output(self) -> str:
        """Combined output, for evidence files and agent diagnosis."""
        parts = []
        if self.stdout.strip():
            parts.append(self.stdout.rstrip())
        if self.stderr.strip():
            parts.append("--- stderr ---\n" + self.stderr.rstrip())
        if self.timed_out:
            parts.append(f"--- timed out after {self.duration_s:.1f}s ---")
        return "\n".join(parts) or "(no output)"

    def tail(self, limit: int = 4000) -> str:
        """Trailing slice of the output, for prompts and summaries."""
        text = self.output()
        if len(text) <= limit:
            return text
        return "...(truncated)...\n" + text[-limit:]


class SandboxError(RuntimeError):
    """Raised when the sandbox itself cannot be prepared."""


class Sandbox(abc.ABC):
    """Runs commands against a workspace without trusting their contents."""

    #: Short backend name, recorded in every report.
    name: str = "base"
    #: True when the backend actually blocks network and filesystem escape.
    strict: bool = False

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    @abc.abstractmethod
    def ensure(self) -> None:
        """Prepare the environment. Idempotent and cached between runs."""

    @abc.abstractmethod
    def run(
        self,
        args: list[str],
        *,
        timeout: int,
        cwd: str | None = None,
    ) -> ExecResult:
        """Run ``args`` in the workspace.

        A leading ``"python"`` is rewritten to the sandbox interpreter, so
        gates can stay backend-agnostic and say ``["python", "-m", "pytest"]``.
        """

    @abc.abstractmethod
    def has_module(self, module: str) -> bool:
        """Whether an importable tool module is present in the environment."""

    def for_workspace(self, workspace: Path) -> "Sandbox":
        """A sandbox of the same kind, prepared the same way, for another
        directory. Used to run mutants against a scratch copy, so the
        project's own files are never modified."""
        return type(self)(workspace)

    #: False when the project's declared dependencies are not installed.
    dependencies_ready: bool = True
    #: One sentence about the execution environment, for the report.
    environment_note: str = ""

    def isolation_notes(self) -> str:
        """Plain-language statement of what this backend does not protect."""
        if self.strict:
            return (
                "Container backend: no network, read-only system paths, "
                "capped CPU and memory, per-command timeout."
            )
        return (
            "Local backend: separate virtualenv and per-command timeout only. "
            "Generated code CAN reach the network and the host filesystem. "
            "Install Docker for enforced isolation."
        )
