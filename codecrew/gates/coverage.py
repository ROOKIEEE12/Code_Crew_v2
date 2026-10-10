"""G3 - branch coverage of the code under test."""

from __future__ import annotations

import json

from ..results import Finding, GateResult, Severity, Status
from .base import Gate, GateContext


class CoverageGate(Gate):
    """Measures *branch* coverage, not line coverage.

    Line coverage is easy to inflate: one happy-path test through a function
    with three branches already reports most lines covered. Branch coverage
    is what exposes the untested error path, which is where AI-generated
    code typically breaks.
    """

    gate_id = "G3"
    name = "Branch coverage"
    blocking = True
    requires_module = "pytest_cov"
    depends_on = ("G2",)

    def check(self, ctx: GateContext) -> GateResult:
        if not ctx.test_files:
            return self._result(ctx, Status.SKIP, "not run",
                                note="no tests to measure coverage from")

        roots = ctx.package_roots()
        report_path = ".codecrew/coverage.json"
        args = ["python", "-m", "pytest", ctx.test_root, "-q",
                "-p", "no:cacheprovider", "--no-header",
                "--cov-branch", f"--cov-report=json:{report_path}"]
        for root in roots:
            args.append(f"--cov={root}")

        run = ctx.sandbox.run(args, timeout=ctx.config.thresholds.command_timeout_s)
        evidence = self.save_evidence(ctx, run)

        data_file = ctx.workspace / report_path
        if not data_file.exists():
            return self._result(
                ctx, Status.ERROR, "coverage report was not produced",
                evidence=evidence, note=run.tail(800),
            )
        try:
            data = json.loads(data_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return self._result(ctx, Status.ERROR, "coverage report unreadable",
                                evidence=evidence, note=str(exc))

        totals = data.get("totals", {})
        percent = float(totals.get("percent_covered", 0.0))
        threshold = ctx.config.thresholds.min_branch_coverage
        findings = _uncovered_findings(data, threshold)
        summary = f"{percent:.1f}% branch coverage (threshold {threshold:.0f}%)"

        if percent + 1e-9 < threshold:
            return self._result(ctx, Status.FAIL, summary, score=percent,
                                findings=findings, evidence=evidence)
        return self._result(ctx, Status.PASS, summary, score=percent,
                            findings=findings, evidence=evidence)


def _uncovered_findings(data: dict, threshold: float) -> list[Finding]:
    """Name the weakest files so a patch can be targeted, not scattershot."""
    findings: list[Finding] = []
    for filename, entry in sorted((data.get("files") or {}).items()):
        summary = entry.get("summary", {})
        percent = float(summary.get("percent_covered", 100.0))
        if percent + 1e-9 >= threshold:
            continue
        missing = summary.get("missing_lines", 0)
        findings.append(Finding(
            severity=Severity.MEDIUM,
            code="low-coverage",
            message=f"{percent:.1f}% covered, {missing} line(s) never executed",
            file=filename,
        ))
    return findings
