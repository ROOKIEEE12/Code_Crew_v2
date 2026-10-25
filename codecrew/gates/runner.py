"""Runs the gate layer over a workspace and aggregates a verdict."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Callable

from ..config import WORK_DIR_NAME, GateConfig
from ..results import GateReport, GateResult, Status
from ..sandbox import Sandbox, get_sandbox
from .base import Gate, GateContext
from .conformance import ConformanceGate
from .coverage import CoverageGate
from .dependencies import DependencyGate
from .mutation import MutationGate
from .properties import PropertyGate
from .security import SecurityGate
from .syntax import SyntaxGate
from .tests import TestGate

#: Execution order. Cheap and broad first, slow and deep last, so that a
#: failing patch gets feedback in seconds rather than minutes.
GATE_CLASSES: tuple[type[Gate], ...] = (
    SyntaxGate,
    DependencyGate,
    ConformanceGate,
    TestGate,
    CoverageGate,
    SecurityGate,
    MutationGate,
    PropertyGate,
)


def all_gate_ids() -> list[str]:
    return [cls.gate_id for cls in GATE_CLASSES]


class GateRunner:
    """Owns one verification pass over a workspace."""

    def __init__(
        self,
        workspace: Path,
        config: GateConfig | None = None,
        sandbox: Sandbox | None = None,
        progress: Callable[[dict], None] | None = None,
        project=None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.config = config or GateConfig()
        self._sandbox = sandbox
        #: codecrew.project.Project: layout and exclusions. Its *settings*
        #: are folded into the config by the caller (see project.load_config),
        #: never silently here.
        self.project = project
        #: Told when each gate starts and finishes. Purely an observer: it
        #: receives copies of the outcome and cannot change it.
        self._progress = progress

    def _notify(self, **event) -> None:
        if self._progress is None:
            return
        try:
            self._progress(event)
        except Exception:  # noqa: BLE001 - a UI must never break a verdict
            pass

    def run(self, only: list[str] | None = None, run_id: str | None = None) -> GateReport:
        run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        started = time.monotonic()

        project = self.project
        if project is None:
            from ..project import Project, ProjectConfigError
            try:
                project = Project.detect(self.workspace)
            except ProjectConfigError:
                project = None  # layout still falls back to the defaults
        sandbox = self._sandbox or get_sandbox(self.workspace, self.config.sandbox,
                                               project=project)
        self._notify(phase="setup", gate="", name="preparing the environment")
        sandbox.ensure()

        evidence_dir = self.workspace / WORK_DIR_NAME / "evidence" / run_id
        ctx = GateContext.build(self.workspace, sandbox, self.config, evidence_dir,
                                project=project)
        report = GateReport(run_id=run_id, workspace=str(self.workspace),
                            sandbox=sandbox.name)
        report.sources = [p.as_posix() for p in ctx.source_files]
        if getattr(sandbox, "environment_note", ""):
            report.notes.append(sandbox.environment_note)
        if project is not None:
            if project.source_root:
                report.notes.append(f"src layout: code under {project.source_root}/")
            if project.settings_file:
                report.notes.append(f"configuration: {project.settings_file}")
            report.notes += project.warnings
        if self.config.disabled:
            report.notes.append(
                "disabled in configuration: " + ", ".join(self.config.disabled))

        statuses: dict[str, Status] = {}
        for gate_cls in GATE_CLASSES:
            gate = gate_cls()
            gate_id = gate.gate_id

            if only and gate_id not in only:
                continue
            if not gate.applicable(ctx):
                continue
            if not self.config.is_enabled(gate_id):
                result = _disabled(gate, self.config)
            elif blocker := _unmet_dependency(gate, statuses):
                result = _blocked(gate, self.config, blocker)
            else:
                self._notify(phase="start", gate=gate_id, name=gate.name)
                result = gate.execute(ctx)
            report.results.append(result)
            statuses[gate_id] = result.status
            self._notify(phase="end", gate=gate_id, name=gate.name,
                         status=result.status.value, summary=result.summary,
                         score=result.score, blocking=result.blocking,
                         note=result.note)

        report.duration_s = time.monotonic() - started
        return report


def _unmet_dependency(gate: Gate, statuses: dict[str, Status]) -> str | None:
    """A dependency counts as unmet unless it explicitly passed."""
    for dep in gate.depends_on:
        if statuses.get(dep) not in (Status.PASS, None):
            return dep
    return None


def _disabled(gate: Gate, config: GateConfig) -> GateResult:
    # ``blocking`` records what the gate *would* have been: a blocking gate
    # that did not run leaves the verdict partial; a SKIP can never block.
    return GateResult(
        gate_id=gate.gate_id, name=gate.name, status=Status.SKIP,
        summary="not run", blocking=config.is_blocking(gate.gate_id, gate.blocking),
        note="disabled in configuration",
    )


def _blocked(gate: Gate, config: GateConfig, blocker: str) -> GateResult:
    return GateResult(
        gate_id=gate.gate_id, name=gate.name, status=Status.SKIP,
        summary="not run", blocking=config.is_blocking(gate.gate_id, gate.blocking),
        note=f"blocked by {blocker}",
    )
