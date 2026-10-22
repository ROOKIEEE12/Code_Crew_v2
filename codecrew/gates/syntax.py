"""G1 - syntax, imports and static type consistency."""

from __future__ import annotations

import json

from ..results import Finding, GateResult, Severity, Status
from .base import Gate, GateContext

#: Ruff rule prefixes that indicate a genuine defect rather than style:
#: syntax/runtime errors (E9) and the pyflakes family (F).
_DEFECT_PREFIXES = ("E9", "F")

#: Pyflakes rules that are hygiene, not correctness. They must not block.
#:
#: Found by benchmark: a correct palindrome solution was rejected as
#: UNVERIFIED for one unused `typing.Iterable` import. Because G1 gates every
#: other check, that also meant its tests never ran, so a correct answer got
#: no signal at all. A gate that blocks on style teaches users to ignore it.
_HYGIENE_ONLY = frozenset({
    "F401",  # imported but unused
    "F841",  # local variable assigned but never used
    "F403",  # `from x import *`
    "F405",  # name may be undefined from star import (noisy companion of F403)
})


class SyntaxGate(Gate):
    """Does the code parse, import cleanly, and type-check?

    Runs first and is strictly blocking: there is no point running tests
    against code that does not compile, and a failure here gives the
    Diagnostician the cheapest possible signal.
    """

    gate_id = "G1"
    name = "Syntax, lint and types"
    blocking = True

    def check(self, ctx: GateContext) -> GateResult:
        runs = []
        findings: list[Finding] = []
        timeout = ctx.config.thresholds.command_timeout_s

        # 1. Compilation: works even with no tooling installed at all.
        compile_run = ctx.sandbox.run(
            ["python", "-m", "compileall", "-q", *ctx.source_args()],
            timeout=timeout,
        )
        runs.append(compile_run)
        if not compile_run.ok:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                code="syntax-error",
                message=compile_run.tail(1200),
            ))
            evidence = self.save_evidence(ctx, runs)
            return self._result(
                ctx, Status.FAIL, "code does not compile",
                findings=findings, evidence=evidence,
            )

        # 2. Ruff: undefined names, unused imports, broken f-strings.
        if ctx.sandbox.has_module("ruff"):
            ruff_run = ctx.sandbox.run(
                ["python", "-m", "ruff", "check", "--output-format", "json",
                 "--no-cache", *ctx.source_args()],
                timeout=timeout,
            )
            runs.append(ruff_run)
            findings.extend(_parse_ruff(ruff_run.stdout))

        # 3. Mypy: signature and return-type mismatches the tests may miss.
        if ctx.sandbox.has_module("mypy"):
            mypy_run = ctx.sandbox.run(
                ["python", "-m", "mypy", "--ignore-missing-imports",
                 "--no-error-summary", "--no-color-output",
                 "--cache-dir", ".codecrew/.mypy_cache", *ctx.source_args()],
                timeout=timeout,
            )
            runs.append(mypy_run)
            findings.extend(_parse_mypy(mypy_run.stdout))

        evidence = self.save_evidence(ctx, runs)
        defects = [f for f in findings if f.severity.rank >= Severity.MEDIUM.rank]
        if defects:
            return self._result(
                ctx, Status.FAIL,
                f"{len(defects)} static defect(s) in {len(ctx.source_files)} file(s)",
                findings=findings, evidence=evidence,
            )
        note = None
        if findings:
            note = f"{len(findings)} non-blocking style finding(s)"
        return self._result(
            ctx, Status.PASS,
            f"{len(ctx.source_files)} file(s) compile and type-check",
            findings=findings, evidence=evidence, note=note,
        )


def _parse_ruff(stdout: str) -> list[Finding]:
    try:
        items = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return []
    findings = []
    for item in items:
        code = item.get("code") or "ruff"
        location = item.get("location") or {}
        is_defect = (
            code.startswith(_DEFECT_PREFIXES) and code not in _HYGIENE_ONLY
        )
        findings.append(Finding(
            severity=Severity.HIGH if is_defect else Severity.LOW,
            code=f"ruff:{code}",
            message=item.get("message", ""),
            file=item.get("filename"),
            line=location.get("row"),
        ))
    return findings


def _parse_mypy(stdout: str) -> list[Finding]:
    findings = []
    for line in (stdout or "").splitlines():
        if ": error:" not in line:
            continue
        head, _, message = line.partition(": error:")
        parts = head.split(":")
        file = parts[0] if parts else None
        lineno = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        message = message.strip()
        # A third-party package without type stubs (or not installed in the
        # tool environment) says nothing about this code. Found on the first
        # real project tried: python-dateutil blocked every other gate.
        # A genuinely broken import still fails G2 when the tests run.
        environment = message.endswith(_ENVIRONMENT_CODES)
        findings.append(Finding(
            severity=Severity.LOW if environment else Severity.MEDIUM,
            code="mypy:missing-stubs" if environment else "mypy:error",
            message=message,
            file=file,
            line=lineno,
        ))
    return findings


_ENVIRONMENT_CODES = ("[import-untyped]", "[import-not-found]", "[import]")
