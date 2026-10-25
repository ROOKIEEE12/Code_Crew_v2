"""G2 - the test suite actually runs, and passes."""

from __future__ import annotations

import re

from ..results import Finding, GateResult, Severity, Status
from .base import Gate, GateContext

_SUMMARY = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed)")
#: pytest exit code for "no tests were collected".
_NO_TESTS = 5


class TestGate(Gate):
    """Executes the test suite inside the sandbox.

    Design note: an absent or empty test suite is a FAIL, not a SKIP. Code
    with no tests is exactly the output this project exists to prevent, so
    it must never reach a passing verdict.
    """

    gate_id = "G2"
    name = "Test execution"
    blocking = True
    requires_module = "pytest"
    depends_on = ("G1",)

    def check(self, ctx: GateContext) -> GateResult:
        if not ctx.test_files:
            return self._result(
                ctx, Status.FAIL, "no test files found",
                findings=[Finding(
                    severity=Severity.HIGH,
                    code="no-tests",
                    message="Workspace contains source code but no tests. "
                            "Unverified code cannot pass the pipeline.",
                )],
            )

        args = ["python", "-m", "pytest", ctx.test_root, "-q",
                "-p", "no:cacheprovider", "--no-header", "-rf"]
        if ctx.sandbox.has_module("pytest_timeout"):
            # Guards against generated code with an infinite loop.
            args += ["--timeout", str(ctx.config.thresholds.test_timeout_s)]

        run = ctx.sandbox.run(args, timeout=ctx.config.thresholds.command_timeout_s)
        evidence = self.save_evidence(ctx, run)
        missing = _missing_module(run.output())
        if not run.ok and missing and not getattr(ctx.sandbox, "dependencies_ready", True):
            # The code was never really tested: the environment could not
            # import a dependency. That is not a verdict on the code, and it
            # must not read like one either way.
            return self._result(
                ctx, Status.ERROR, "tests could not run: a dependency is missing",
                evidence=evidence,
                note=f"No module named {missing!r}. "
                     + getattr(ctx.sandbox, "environment_note", ""),
            )
        counts = _parse_counts(run.output())
        passed = counts.get("passed", 0)
        failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)

        if run.timed_out:
            return self._result(
                ctx, Status.FAIL, "test run timed out", evidence=evidence,
                findings=[Finding(
                    severity=Severity.HIGH, code="test-timeout",
                    message="Suite exceeded the time limit; suspect a hang or "
                            "an infinite loop in the generated code.",
                )],
                note="raised the possibility of non-termination",
            )
        if run.returncode == _NO_TESTS:
            return self._result(
                ctx, Status.FAIL, "pytest collected no tests", evidence=evidence,
                findings=[Finding(
                    severity=Severity.HIGH, code="no-tests-collected",
                    message="Test files exist but contain no runnable tests.",
                )],
            )
        if not run.ok:
            return self._result(
                ctx, Status.FAIL,
                f"{failed} failing, {passed} passing" if failed
                else f"test run failed (exit {run.returncode})",
                findings=_failure_findings(run.output()),
                evidence=evidence,
            )
        return self._result(
            ctx, Status.PASS, f"{passed} test(s) passed",
            score=float(passed), evidence=evidence,
        )


_MISSING = re.compile(r"No module named '([\w.]+)'")


def _missing_module(output: str) -> str | None:
    match = _MISSING.search(output)
    return match.group(1) if match else None


def _parse_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for count, label in _SUMMARY.findall(output):
        counts[label] = counts.get(label, 0) + int(count)
    return counts


def _failure_findings(output: str) -> list[Finding]:
    """Turn the short test summary into located findings.

    These feed the Diagnostician in Phase 3, which is why the file and line
    are extracted rather than handing over a wall of text.
    """
    findings: list[Finding] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("FAILED", "ERROR")):
            continue
        _, _, rest = stripped.partition(" ")
        target, _, reason = rest.partition(" - ")
        file, _, test = target.partition("::")
        findings.append(Finding(
            severity=Severity.HIGH,
            code="test-failure",
            message=f"{test or target}: {reason.strip() or 'failed'}",
            file=file or None,
        ))
    if not findings:
        findings.append(Finding(
            severity=Severity.HIGH,
            code="test-failure",
            message="Test run exited non-zero; see evidence log for details.",
        ))
    return findings
