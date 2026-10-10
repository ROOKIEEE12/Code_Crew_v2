"""Runs the gate layer over a workspace and aggregates a verdict."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

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
    ) -> None:
        self.workspace = workspace.resolve()
        self.config = config or GateConfig()
        self._sandbox = sandbox

    def run(self, only: list[str] | None = None, run_id: str | None = None) -> GateReport:
        run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        started = time.monotonic()

        sandbox = self._sandbox or get_sandbox(self.workspace, self.config.sandbox)
        sandbox.ensure()

        evidence_dir = self.workspace / WORK_DIR_NAME / "evidence" / run_id
        ctx = GateContext.build(self.workspace, sandbox, self.config, evidence_dir)
        report = GateReport(run_id=run_id, workspace=str(self.workspace),
                            sandbox=sandbox.name)

        statuses: dict[str, Status] = {}
        for gate_cls in GATE_CLASSES:
            gate = gate_cls()
            gate_id = gate.gate_id

            if only and gate_id not in only:
                continue
            if not gate.applicable(ctx):
                continue
            if not self.config.is_enabled(gate_id):
                report.results.append(_disabled(gate, self.config))
                statuses[gate_id] = Status.SKIP
                continue

            blocker = _unmet_dependency(gate, statuses)
            if blocker:
                report.results.append(_blocked(gate, self.config, blocker))
                statuses[gate_id] = Status.SKIP
                continue

            result = gate.execute(ctx)
            report.results.append(result)
            statuses[gate_id] = result.status

        report.duration_s = time.monotonic() - started
        return report


def _unmet_dependency(gate: Gate, statuses: dict[str, Status]) -> str | None:
    """A dependency counts as unmet unless it explicitly passed."""
    for dep in gate.depends_on:
        if statuses.get(dep) not in (Status.PASS, None):
            return dep
    return None


def _disabled(gate: Gate, config: GateConfig) -> GateResult:
    return GateResult(
        gate_id=gate.gate_id, name=gate.name, status=Status.SKIP,
        summary="not run", blocking=False,
        note="disabled in configuration",
    )


def _blocked(gate: Gate, config: GateConfig, blocker: str) -> GateResult:
    return GateResult(
        gate_id=gate.gate_id, name=gate.name, status=Status.SKIP,
        summary="not run", blocking=False,
        note=f"blocked by {blocker}",
    )
