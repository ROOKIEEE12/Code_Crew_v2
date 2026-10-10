"""Result types for the gate layer.

Deliberately stdlib-only dataclasses: the gate layer must be runnable with a
bare Python install, because it is the thing that judges everything else.
Agent-facing contracts (Phase 1+) use Pydantic instead, where validation of
untrusted LLM output is the point.
"""

from __future__ import annotations

import dataclasses
import enum
import json
from dataclasses import dataclass, field
from pathlib import Path


class Status(str, enum.Enum):
    """Outcome of a single gate."""

    PASS = "PASS"
    FAIL = "FAIL"
    #: Tool unavailable or nothing to check. Never counts as success.
    SKIP = "SKIP"
    #: The gate itself broke. Distinct from FAIL so bugs in CodeCrew are not
    #: silently reported as defects in the code under test.
    ERROR = "ERROR"


class Severity(str, enum.Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


@dataclass
class Finding:
    """One concrete, located problem reported by a tool."""

    severity: Severity
    code: str
    message: str
    file: str | None = None
    line: int | None = None

    def location(self) -> str:
        if self.file and self.line:
            return f"{self.file}:{self.line}"
        return self.file or "-"


@dataclass
class GateResult:
    """What a gate concluded, and the evidence it concluded it from.

    ``evidence`` is the path to the raw tool output. The orchestrator will
    later refuse to let any agent claim success without pointing at one of
    these, which is how we stop agents from grading their own homework.
    """

    gate_id: str
    name: str
    status: Status
    summary: str
    #: Scored gates (coverage, mutation) report a percentage here.
    score: float | None = None
    findings: list[Finding] = field(default_factory=list)
    duration_s: float = 0.0
    blocking: bool = True
    evidence: str | None = None
    #: Why a gate was skipped, or what broke when it errored.
    note: str | None = None

    @property
    def blocked(self) -> bool:
        """True when this result must stop the pipeline."""
        return self.blocking and self.status in (Status.FAIL, Status.ERROR)

    def worst_severity(self) -> Severity | None:
        if not self.findings:
            return None
        return max((f.severity for f in self.findings), key=lambda s: s.rank)


@dataclass
class GateReport:
    """The aggregate verdict over one workspace."""

    run_id: str
    workspace: str
    sandbox: str
    results: list[GateResult] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def passed(self) -> bool:
        """A run passes only if nothing blocking failed.

        Note that SKIP never counts as a pass; it is surfaced separately as
        "not verified" so the final report can be honest about coverage of
        the verification itself.
        """
        return not any(r.blocked for r in self.results)

    @property
    def skipped(self) -> list[GateResult]:
        return [r for r in self.results if r.status is Status.SKIP]

    def counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in Status}
        for r in self.results:
            counts[r.status.value] += 1
        return counts

    def verdict(self) -> str:
        """Trust badge for the workspace as a whole."""
        if not self.passed:
            return "UNVERIFIED"
        if self.skipped:
            return "PARTIALLY VERIFIED"
        return "VERIFIED"

    def to_dict(self) -> dict:
        return dataclasses.asdict(self, dict_factory=_json_dict_factory)

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_dict()
        payload["passed"] = self.passed
        payload["verdict"] = self.verdict()
        payload["counts"] = self.counts()
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _json_dict_factory(items: list[tuple[str, object]]) -> dict:
    """Render enums as their string values so the JSON is readable."""
    out = {}
    for key, value in items:
        out[key] = value.value if isinstance(value, enum.Enum) else value
    return out
