"""Calibration tests for gate severity.

A gate that blocks on style teaches users to ignore it, and because G1 gates
the tests, a spurious G1 failure also hides every other signal. These pin
down which findings may block.
"""

from __future__ import annotations

import json

import pytest

from codecrew.gates.syntax import _parse_ruff
from codecrew.results import Severity


def ruff_output(code: str, message: str = "m") -> str:
    return json.dumps([{"code": code, "message": message,
                        "filename": "app.py", "location": {"row": 3}}])


@pytest.mark.parametrize("code", ["F401", "F841", "F403", "F405"])
def test_hygiene_findings_do_not_block(code):
    (finding,) = _parse_ruff(ruff_output(code))
    assert finding.severity.rank < Severity.MEDIUM.rank


@pytest.mark.parametrize("code", ["F821", "F811", "F632", "F601", "E902", "F541"])
def test_real_defects_still_block(code):
    (finding,) = _parse_ruff(ruff_output(code))
    assert finding.severity.rank >= Severity.MEDIUM.rank


def test_a_correct_module_with_an_unused_import_passes_g1(tmp_path):
    """The regression, end to end: this exact shape produced the false alarm."""
    from codecrew.config import GateConfig
    from codecrew.gates import GateRunner
    from codecrew.results import Status

    (tmp_path / "palindrome.py").write_text(
        "from __future__ import annotations\n\n"
        "from typing import Iterable\n\n\n"
        "def is_palindrome(text: str) -> bool:\n"
        "    cleaned = [c.lower() for c in text if c.isalnum()]\n"
        "    return cleaned == cleaned[::-1]\n",
        encoding="utf-8",
    )
    report = GateRunner(tmp_path, GateConfig(sandbox="local")).run(only=["G1"])
    (g1,) = report.results
    assert g1.status is Status.PASS
    assert any(f.code == "ruff:F401" for f in g1.findings), \
        "still reported, just not blocking"
