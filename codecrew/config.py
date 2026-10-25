"""Configuration for the gate layer.

Thresholds live here, not inside the gates, so an examiner (or an ablation
study) can change the strictness of the pipeline without touching logic.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: Directory created inside a workspace to hold the venv, evidence and reports.
WORK_DIR_NAME = ".codecrew"

#: Tools installed into the sandbox environment. The gates invoke these as
#: ``python -m <module>``, so the sandbox needs them regardless of backend.
SANDBOX_REQUIREMENTS: tuple[str, ...] = (
    "pytest==8.3.3",
    "pytest-cov==5.0.0",
    "pytest-timeout==2.3.1",
    "hypothesis==6.112.1",
    "ruff==0.6.9",
    "mypy==1.11.2",
    "bandit==1.7.10",
    "mutmut==2.5.0",
    "detect-secrets==1.5.0",
)


@dataclass
class Thresholds:
    """Pass/fail lines for the scored gates."""

    #: Minimum branch coverage, as a percentage, for G3 to pass.
    min_branch_coverage: float = 70.0
    #: Minimum mutation score, as a percentage, for G4 to pass. This is the
    #: number that distinguishes real tests from tests written to pass.
    min_mutation_score: float = 50.0
    #: Security findings at or above this severity fail G5.
    max_security_severity: str = "medium"
    #: Per-command wall clock limit inside the sandbox.
    command_timeout_s: int = 300
    #: Per-test limit (pytest-timeout), so one hung test is a failure, not a
    #: stalled run.
    test_timeout_s: int = 60
    #: Mutation testing is slow; cap the whole gate and each mutant run.
    mutation_timeout_s: int = 900
    mutant_timeout_s: int = 60
    #: Upper bound on mutants evaluated per run. Sites beyond this are
    #: sampled deterministically so scores stay comparable across runs.
    max_mutants: int = 40


@dataclass
class GateConfig:
    """Which gates run, and how hard they push back."""

    thresholds: Thresholds = field(default_factory=Thresholds)
    #: Gate ids to skip entirely, e.g. ("G4",) while iterating locally.
    disabled: tuple[str, ...] = ()
    #: Gate ids whose failure must not block the verdict. Useful for the
    #: ablation study: disabling blocking turns a gate into a pure observer.
    non_blocking: tuple[str, ...] = ()
    #: Preferred sandbox backend: "auto", "docker" or "local".
    sandbox: str = "auto"

    def is_enabled(self, gate_id: str) -> bool:
        return gate_id not in self.disabled

    def is_blocking(self, gate_id: str, default: bool) -> bool:
        if gate_id in self.non_blocking:
            return False
        return default

    @classmethod
    def load(cls, path: Path | None) -> "GateConfig":
        """Load config from JSON, falling back to defaults."""
        if path is None or not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        thresholds = Thresholds(**raw.pop("thresholds", {}))
        return cls(
            thresholds=thresholds,
            disabled=tuple(raw.pop("disabled", ())),
            non_blocking=tuple(raw.pop("non_blocking", ())),
            sandbox=raw.pop("sandbox", "auto"),
        )

    def to_dict(self) -> dict:
        return asdict(self)
