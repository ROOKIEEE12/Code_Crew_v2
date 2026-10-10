"""G6 - property-based test coverage.

Example-based tests check the inputs the author thought of, which for an LLM
means the inputs it also used to write the code. Property-based tests
(Hypothesis) search the input space instead, and are what catch the empty
string, the zero, the negative number and the unicode surrogate.

This gate measures how many public functions are exercised by at least one
property test. It is non-blocking: it reports a gap rather than halting the
pipeline, and in Phase 2 that gap is what the Test-Author agent is asked to
close.
"""

from __future__ import annotations

import ast
from pathlib import Path

from ..results import Finding, GateResult, Severity, Status
from .base import Gate, GateContext

_HYPOTHESIS_DECORATORS = {"given", "example"}


class PropertyGate(Gate):
    gate_id = "G6"
    name = "Property-based coverage"
    blocking = False

    def check(self, ctx: GateContext) -> GateResult:
        public = _public_functions(ctx.workspace, ctx.source_files)
        if not public:
            return self._result(ctx, Status.SKIP, "not run",
                                note="no public functions to exercise")

        exercised = _property_tested_names(ctx.workspace, ctx.test_files)
        covered = sorted(name for name in public if name in exercised)
        missing = sorted(name for name in public if name not in exercised)
        score = 100.0 * len(covered) / len(public)

        findings = [
            Finding(
                severity=Severity.LOW,
                code="no-property-test",
                message=f"'{name}' has no property-based test; edge-case "
                        f"behaviour rests on hand-picked examples only",
                file=public[name],
            )
            for name in missing
        ]
        summary = (
            f"{score:.0f}% of public functions have a property test "
            f"({len(covered)}/{len(public)})"
        )
        if not exercised:
            return self._result(
                ctx, Status.SKIP, summary, score=score, findings=findings,
                note="no Hypothesis @given tests found in the suite",
            )
        status = Status.PASS if not missing else Status.FAIL
        return self._result(ctx, status, summary, score=score, findings=findings)


def _public_functions(workspace: Path, files: list[Path]) -> dict[str, str]:
    """Map public function name -> file it is defined in."""
    found: dict[str, str] = {}
    for rel in files:
        try:
            tree = ast.parse((workspace / rel).read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_"):
                continue
            found.setdefault(node.name, rel.as_posix())
    return found


def _property_tested_names(workspace: Path, test_files: list[Path]) -> set[str]:
    """Names referenced inside Hypothesis-decorated test functions."""
    names: set[str] = set()
    for rel in test_files:
        try:
            tree = ast.parse((workspace / rel).read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _has_hypothesis_decorator(node):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Name):
                    names.add(child.id)
                elif isinstance(child, ast.Attribute):
                    names.add(child.attr)
    return names


def _has_hypothesis_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = target.attr if isinstance(target, ast.Attribute) else getattr(
            target, "id", None)
        if name in _HYPOTHESIS_DECORATORS:
            return True
    return False
