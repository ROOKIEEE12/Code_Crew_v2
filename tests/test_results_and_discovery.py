"""Tests for verdict logic and workspace discovery.

The verdict rules encode the project's central policy decisions, so they are
tested directly rather than only through the CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codecrew.config import GateConfig, Thresholds
from codecrew.gates.base import GateContext
from codecrew.results import Finding, GateReport, GateResult, Severity, Status


def result(gate_id: str, status: Status, blocking: bool = True) -> GateResult:
    return GateResult(gate_id=gate_id, name=gate_id, status=status,
                      summary="", blocking=blocking)


def report(*results: GateResult) -> GateReport:
    return GateReport(run_id="t", workspace="w", sandbox="local-venv",
                      results=list(results))


# -- verdict policy -----------------------------------------------------------

def test_all_pass_is_verified():
    assert report(result("G1", Status.PASS)).verdict() == "VERIFIED"


def test_a_skip_downgrades_to_partially_verified():
    """A skipped gate must never be presentable as a clean pass."""
    rep = report(result("G1", Status.PASS), result("G4", Status.SKIP))
    assert rep.passed is True
    assert rep.verdict() == "PARTIALLY VERIFIED"


def test_blocking_failure_is_unverified():
    rep = report(result("G1", Status.PASS), result("G5", Status.FAIL))
    assert rep.passed is False
    assert rep.verdict() == "UNVERIFIED"


def test_gate_error_blocks_too():
    """A crashed gate is unknown, not acceptable."""
    rep = report(result("G3", Status.ERROR))
    assert rep.verdict() == "UNVERIFIED"


def test_non_blocking_failure_does_not_block():
    rep = report(result("G6", Status.FAIL, blocking=False))
    assert rep.passed is True


def test_worst_severity_ranks_by_order_not_alphabet():
    res = result("G5", Status.FAIL)
    res.findings = [
        Finding(Severity.HIGH, "a", "x"),
        Finding(Severity.LOW, "b", "y"),
        Finding(Severity.CRITICAL, "c", "z"),
    ]
    assert res.worst_severity() is Severity.CRITICAL


def test_report_json_round_trips(tmp_path: Path):
    res = result("G5", Status.FAIL)
    res.findings = [Finding(Severity.HIGH, "secret:aws", "hardcoded key",
                            "app.py", 12)]
    target = tmp_path / "r.json"
    report(res).write_json(target)
    import json
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["verdict"] == "UNVERIFIED"
    assert data["passed"] is False
    # Enums must serialise as readable strings, not Python reprs.
    assert data["results"][0]["findings"][0]["severity"] == "high"
    assert data["results"][0]["status"] == "FAIL"


# -- configuration ------------------------------------------------------------

def test_non_blocking_config_overrides_gate_default():
    config = GateConfig(non_blocking=("G4",))
    assert config.is_blocking("G4", default=True) is False
    assert config.is_blocking("G5", default=True) is True


def test_disabled_gates_are_reported_not_hidden():
    config = GateConfig(disabled=("G4",))
    assert config.is_enabled("G4") is False
    assert config.is_enabled("G2") is True


def test_config_loads_thresholds_from_json(tmp_path: Path):
    path = tmp_path / "c.json"
    path.write_text('{"thresholds": {"min_mutation_score": 80}, '
                    '"disabled": ["G6"]}', encoding="utf-8")
    config = GateConfig.load(path)
    assert config.thresholds.min_mutation_score == 80
    assert config.disabled == ("G6",)


def test_missing_config_falls_back_to_defaults(tmp_path: Path):
    config = GateConfig.load(tmp_path / "absent.json")
    assert config.thresholds == Thresholds()


# -- workspace discovery ------------------------------------------------------

@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "core.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "helper.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_core.py").write_text("", encoding="utf-8")
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    # Our own scaffolding, plus a stale venv: neither is code under test.
    (tmp_path / ".codecrew" / "evidence").mkdir(parents=True)
    (tmp_path / ".codecrew" / "junk.py").write_text("z = 3\n", encoding="utf-8")
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "lib.py").write_text("w = 4\n", encoding="utf-8")
    return tmp_path


def context_for(workspace: Path) -> GateContext:
    return GateContext.build(workspace, sandbox=None, config=GateConfig(),
                             evidence_dir=workspace / ".codecrew" / "evidence")


def test_discovery_separates_sources_from_tests(workspace: Path):
    ctx = context_for(workspace)
    sources = {p.as_posix() for p in ctx.source_files}
    tests = {p.as_posix() for p in ctx.test_files}
    assert sources == {"app/__init__.py", "app/core.py", "helper.py"}
    assert tests == {"conftest.py", "tests/test_core.py"}


def test_discovery_excludes_scaffolding_and_venvs(workspace: Path):
    ctx = context_for(workspace)
    joined = " ".join(p.as_posix() for p in ctx.source_files)
    assert ".codecrew" not in joined
    assert "venv" not in joined


def test_package_roots_are_importable_names(workspace: Path):
    ctx = context_for(workspace)
    assert ctx.package_roots() == ["app", "helper"]


def test_test_root_prefers_the_tests_directory(workspace: Path):
    assert context_for(workspace).test_root == "tests"


def test_test_root_falls_back_to_the_workspace(tmp_path: Path):
    (tmp_path / "m.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "test_m.py").write_text("", encoding="utf-8")
    assert context_for(tmp_path).test_root == "."
