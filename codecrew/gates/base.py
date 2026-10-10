"""Gate contract and shared helpers.

A gate is deterministic: it shells out to a real tool and reports what the
tool said. No gate is allowed to contain an LLM call, now or later. That
constraint is the whole reason the pipeline can claim anything at all.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import WORK_DIR_NAME, GateConfig
from ..results import GateResult, Status
from ..sandbox import ExecResult, Sandbox

#: Paths excluded from analysis: our own scaffolding, not code under test.
EXCLUDED_DIRS = {WORK_DIR_NAME, ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
                 ".ruff_cache", "venv", ".venv", "node_modules", ".mutmut-cache"}


@dataclass
class GateContext:
    """Everything a gate needs to judge one workspace."""

    workspace: Path
    sandbox: Sandbox
    config: GateConfig
    evidence_dir: Path
    #: Populated by :meth:`discover`.
    source_files: list[Path] = field(default_factory=list)
    test_files: list[Path] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        workspace: Path,
        sandbox: Sandbox,
        config: GateConfig,
        evidence_dir: Path,
    ) -> "GateContext":
        ctx = cls(workspace.resolve(), sandbox, config, evidence_dir)
        ctx.discover()
        return ctx

    def discover(self) -> None:
        """Split the workspace into code under test and its tests."""
        sources: list[Path] = []
        tests: list[Path] = []
        for path in sorted(self.workspace.rglob("*.py")):
            rel = path.relative_to(self.workspace)
            if any(part in EXCLUDED_DIRS for part in rel.parts):
                continue
            if _is_test(rel):
                tests.append(rel)
            else:
                sources.append(rel)
        self.source_files = sources
        self.test_files = tests

    @property
    def test_root(self) -> str:
        """Directory to point pytest at."""
        return "tests" if (self.workspace / "tests").is_dir() else "."

    def source_args(self) -> list[str]:
        """Source paths as POSIX strings, for passing to tools."""
        return [p.as_posix() for p in self.source_files]

    def package_roots(self) -> list[str]:
        """Top-level importable names of the code under test.

        Used for coverage and mutation targeting: a package directory if the
        project has one, otherwise the individual top-level modules.
        """
        roots: list[str] = []
        for rel in self.source_files:
            top = rel.parts[0]
            if top.endswith(".py"):
                name = top[:-3]
                if name != "__init__" and name not in roots:
                    roots.append(name)
            elif top not in roots:
                roots.append(top)
        return roots


def _is_test(rel: Path) -> bool:
    if "tests" in rel.parts or "test" in rel.parts:
        return True
    name = rel.name
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


class Gate(abc.ABC):
    """One deterministic check."""

    #: Stable id used in configs, reports and the ablation study.
    gate_id: str = "G?"
    name: str = "unnamed"
    #: Whether failing this gate stops the pipeline by default.
    blocking: bool = True
    #: Tool module that must be importable in the sandbox, if any.
    requires_module: str | None = None
    #: Gates that must have passed first. A gate whose dependency failed is
    #: reported as SKIP ("blocked by G1") rather than run against code that
    #: cannot compile - but independent gates still run, so one failure does
    #: not hide every other problem from the Diagnostician.
    depends_on: tuple[str, ...] = ()

    def applicable(self, ctx: GateContext) -> bool:
        """Whether this gate has anything to say about this workspace.

        Distinct from a skip: a skipped gate is *unverified*, a gate that
        does not apply simply is not part of the run.
        """
        return True

    def precondition(self, ctx: GateContext) -> str | None:
        """Return a skip reason, or None when the gate can run."""
        if self.requires_module and not ctx.sandbox.has_module(self.requires_module):
            return f"{self.requires_module} is not installed in the sandbox"
        if not ctx.source_files:
            return "no Python source files found in the workspace"
        return None

    @abc.abstractmethod
    def check(self, ctx: GateContext) -> GateResult:
        """Run the tool and interpret its output."""

    def execute(self, ctx: GateContext) -> GateResult:
        """Run with timing, skip handling and crash containment."""
        started = time.monotonic()
        reason = self.precondition(ctx)
        if reason is not None:
            return self._result(ctx, Status.SKIP, "not run", note=reason,
                                duration=time.monotonic() - started)
        try:
            result = self.check(ctx)
        except Exception as exc:  # a broken gate must not look like clean code
            return self._result(
                ctx, Status.ERROR, f"gate crashed: {type(exc).__name__}",
                note=str(exc), duration=time.monotonic() - started,
            )
        result.duration_s = time.monotonic() - started
        result.blocking = ctx.config.is_blocking(self.gate_id, self.blocking)
        return result

    # -- helpers for subclasses ------------------------------------------------

    def _result(
        self,
        ctx: GateContext,
        status: Status,
        summary: str,
        *,
        score: float | None = None,
        findings: list | None = None,
        evidence: str | None = None,
        note: str | None = None,
        duration: float = 0.0,
    ) -> GateResult:
        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            status=status,
            summary=summary,
            score=score,
            findings=findings or [],
            duration_s=duration,
            blocking=ctx.config.is_blocking(self.gate_id, self.blocking),
            evidence=evidence,
            note=note,
        )

    def save_evidence(self, ctx: GateContext, results: ExecResult | list[ExecResult],
                      suffix: str = "") -> str:
        """Write raw tool output to disk and return its relative path.

        Agents may only assert a gate outcome by citing one of these files.
        """
        if isinstance(results, ExecResult):
            results = [results]
        ctx.evidence_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{self.gate_id}{suffix}.log"
        target = ctx.evidence_dir / filename
        blocks = []
        for res in results:
            blocks.append(
                f"$ {' '.join(res.command)}\n"
                f"exit={res.returncode} duration={res.duration_s:.2f}s\n\n"
                f"{res.output()}"
            )
        target.write_text("\n\n" + ("=" * 72 + "\n").join(blocks), encoding="utf-8")
        try:
            return target.relative_to(ctx.workspace).as_posix()
        except ValueError:
            return target.as_posix()
