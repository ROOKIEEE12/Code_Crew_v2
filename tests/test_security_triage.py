"""Bandit findings are triaged by confidence as well as severity."""

from __future__ import annotations

import json

import pytest

from codecrew.gates.security import _parse_bandit
from codecrew.results import Severity


def bandit(severity: str, confidence: str, test_id: str = "B000") -> str:
    return json.dumps({"results": [{
        "issue_severity": severity, "issue_confidence": confidence,
        "test_id": test_id, "issue_text": "t",
        "filename": "app.py", "line_number": 7,
    }]})


@pytest.mark.parametrize("severity", ["MEDIUM", "LOW"])
def test_low_confidence_findings_below_high_do_not_block(severity):
    (finding,) = _parse_bandit(bandit(severity, "LOW", "B608"))
    assert finding.severity is Severity.LOW


def test_high_severity_is_never_downgraded_by_low_confidence():
    """A possibly-real HIGH finding must still stop the pipeline."""
    (finding,) = _parse_bandit(bandit("HIGH", "LOW"))
    assert finding.severity is Severity.HIGH


@pytest.mark.parametrize("confidence", ["MEDIUM", "HIGH"])
def test_confident_medium_findings_still_block(confidence):
    """eval() is MEDIUM severity, HIGH confidence, and must stay blocking."""
    (finding,) = _parse_bandit(bandit("MEDIUM", confidence, "B307"))
    assert finding.severity is Severity.MEDIUM


def test_the_insecure_sample_is_still_rejected(tmp_path):
    """The calibration must not have weakened the headline demo."""
    import shutil
    from pathlib import Path

    from codecrew.config import GateConfig
    from codecrew.gates import GateRunner
    from codecrew.results import Status

    sample = Path(__file__).resolve().parents[1] / "samples" / "strength_vibe"
    shutil.copytree(sample, tmp_path / "ws", ignore=shutil.ignore_patterns(
        ".codecrew", "__pycache__", ".hypothesis"))
    report = GateRunner(tmp_path / "ws", GateConfig(sandbox="local")).run(
        only=["G1", "G5"])
    g5 = next(r for r in report.results if r.gate_id == "G5")
    assert g5.status is Status.FAIL
    codes = " ".join(f.code for f in g5.findings if f.severity.rank >= 2)
    assert "bandit:B307" in codes and "secret" in codes
