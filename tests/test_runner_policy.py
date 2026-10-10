"""Tests for gate sequencing and skip-propagation."""

from __future__ import annotations

from pathlib import Path

from codecrew.config import GateConfig
from codecrew.gates.coverage import CoverageGate
from codecrew.gates.runner import GATE_CLASSES, GateRunner, _unmet_dependency
from codecrew.gates.security import SecurityGate
from codecrew.gates.syntax import SyntaxGate
from codecrew.gates.tests import TestGate
from codecrew.results import Status


def test_dependency_is_unmet_unless_it_passed():
    gate = CoverageGate()
    assert _unmet_dependency(gate, {"G2": Status.PASS}) is None
    assert _unmet_dependency(gate, {"G2": Status.FAIL}) == "G2"
    assert _unmet_dependency(gate, {"G2": Status.SKIP}) == "G2"
    # A skipped dependency must not be treated as success: that is how a
    # missing tool would otherwise turn into a silent pass.
    assert _unmet_dependency(gate, {"G2": Status.ERROR}) == "G2"


def test_gate_with_no_dependencies_always_runs():
    assert _unmet_dependency(SyntaxGate(), {}) is None


def test_cheap_gates_run_before_expensive_ones():
    """Ordering matters: a failing patch should get feedback in seconds."""
    order = [cls.gate_id for cls in GATE_CLASSES]
    assert order.index("G1") < order.index("G2") < order.index("G4")
    assert order.index("G5") < order.index("G4"), \
        "security is cheap and must not wait behind mutation testing"


def test_every_gate_id_is_unique():
    ids = [cls.gate_id for cls in GATE_CLASSES]
    assert len(ids) == len(set(ids))


def test_no_gate_depends_on_a_later_gate():
    seen: set[str] = set()
    for cls in GATE_CLASSES:
        for dep in cls.depends_on:
            assert dep in seen, f"{cls.gate_id} depends on later gate {dep}"
        seen.add(cls.gate_id)


def test_broken_code_blocks_downstream_gates(tmp_path: Path):
    """End-to-end: a syntax error must not produce a passing verdict."""
    (tmp_path / "broken.py").write_text("def f(:\n    return 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_broken.py").write_text(
        "def test_nothing():\n    assert True\n", encoding="utf-8")

    config = GateConfig(sandbox="local")
    report = GateRunner(tmp_path, config).run()
    by_id = {r.gate_id: r for r in report.results}

    assert by_id["G1"].status is Status.FAIL
    assert by_id["G2"].status is Status.SKIP
    assert by_id["G2"].note == "blocked by G1"
    assert report.verdict() == "UNVERIFIED"
    assert report.passed is False


def test_code_without_tests_fails_rather_than_skips(tmp_path: Path):
    """The policy that matters most: unverified code never passes."""
    (tmp_path / "app.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")

    config = GateConfig(sandbox="local")
    report = GateRunner(tmp_path, config).run(only=["G1", "G2"])
    by_id = {r.gate_id: r for r in report.results}

    assert by_id["G1"].status is Status.PASS
    assert by_id["G2"].status is Status.FAIL
    assert "no test" in by_id["G2"].summary
    assert report.passed is False


def test_gate_crash_is_reported_as_error_not_pass(monkeypatch, tmp_path: Path):
    """A bug in CodeCrew must never read as clean code."""
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    class Boom(SecurityGate):
        def check(self, ctx):
            raise RuntimeError("tool exploded")

    from codecrew.gates.base import GateContext

    ctx = GateContext.build(tmp_path, sandbox=_StubSandbox(tmp_path),
                            config=GateConfig(), evidence_dir=tmp_path / "ev")
    result = Boom().execute(ctx)
    assert result.status is Status.ERROR
    assert "tool exploded" in (result.note or "")


class _StubSandbox:
    """Minimal sandbox stand-in: no tools, nothing executed."""

    name = "stub"
    strict = False

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def ensure(self) -> None:
        pass

    def has_module(self, module: str) -> bool:
        return True

    def run(self, args, *, timeout, cwd=None):
        raise AssertionError("stub sandbox must not execute anything")


def test_missing_tool_is_skipped_with_a_reason(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    class NoTools(_StubSandbox):
        def has_module(self, module: str) -> bool:
            return False

    from codecrew.gates.base import GateContext

    ctx = GateContext.build(tmp_path, sandbox=NoTools(tmp_path),
                            config=GateConfig(), evidence_dir=tmp_path / "ev")
    result = TestGate().execute(ctx)
    assert result.status is Status.SKIP
    assert "pytest" in (result.note or "")
