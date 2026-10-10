"""G5 - security scanning.

Motivated directly by the evidence that this is where AI-generated code is
weakest and not improving: roughly 44-45% of generated samples fail security
tests, with hardcoded credentials and authorization flaws dominant, and
security *degrading* across refinement iterations. That last finding is why
the runner re-runs this gate after every patch rather than only once.
"""

from __future__ import annotations

import json

from ..results import Finding, GateResult, Severity, Status
from .base import Gate, GateContext

_BANDIT_SEVERITY = {
    "LOW": Severity.LOW,
    "MEDIUM": Severity.MEDIUM,
    "HIGH": Severity.HIGH,
}


class SecurityGate(Gate):
    gate_id = "G5"
    name = "Security scan"
    blocking = True
    depends_on = ("G1",)

    def check(self, ctx: GateContext) -> GateResult:
        runs = []
        findings: list[Finding] = []
        timeout = ctx.config.thresholds.command_timeout_s
        tools_run: list[str] = []

        if ctx.sandbox.has_module("bandit"):
            run = ctx.sandbox.run(
                ["python", "-m", "bandit", "-f", "json", "-q",
                 "-r", *ctx.source_args()],
                timeout=timeout,
            )
            runs.append(run)
            tools_run.append("bandit")
            findings.extend(_parse_bandit(run.stdout))

        if ctx.sandbox.has_module("detect_secrets"):
            run = ctx.sandbox.run(
                ["python", "-m", "detect_secrets", "scan", *ctx.source_args()],
                timeout=timeout,
            )
            runs.append(run)
            tools_run.append("detect-secrets")
            findings.extend(_parse_detect_secrets(run.stdout))

        if not tools_run:
            return self._result(
                ctx, Status.SKIP, "not run",
                note="no security scanner available in the sandbox "
                     "(expected bandit and/or detect-secrets)",
            )

        evidence = self.save_evidence(ctx, runs)
        limit = Severity(ctx.config.thresholds.max_security_severity)
        blocking_findings = [f for f in findings if f.severity.rank >= limit.rank]
        tool_list = " + ".join(tools_run)

        if blocking_findings:
            worst = max((f.severity for f in blocking_findings),
                        key=lambda s: s.rank)
            return self._result(
                ctx, Status.FAIL,
                f"{len(blocking_findings)} security finding(s) at or above "
                f"{limit.value} (worst: {worst.value})",
                findings=findings, evidence=evidence,
                note=f"scanned with {tool_list}",
            )
        return self._result(
            ctx, Status.PASS,
            f"no findings at or above {limit.value}",
            findings=findings, evidence=evidence,
            note=f"scanned with {tool_list}"
                 + (f"; {len(findings)} informational finding(s)" if findings else ""),
        )


def _parse_bandit(stdout: str) -> list[Finding]:
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return []
    findings = []
    for item in data.get("results", []):
        severity = _BANDIT_SEVERITY.get(item.get("issue_severity", "LOW"), Severity.LOW)
        findings.append(Finding(
            severity=severity,
            code=f"bandit:{item.get('test_id', '?')}",
            message=item.get("issue_text", "").strip(),
            file=item.get("filename"),
            line=item.get("line_number"),
        ))
    return findings


def _parse_detect_secrets(stdout: str) -> list[Finding]:
    """Hardcoded credentials are treated as HIGH without exception.

    This is the single most common AI-generated security defect, and it is
    never acceptable in delivered code regardless of context.
    """
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return []
    findings = []
    for filename, entries in (data.get("results") or {}).items():
        for entry in entries:
            findings.append(Finding(
                severity=Severity.HIGH,
                code=f"secret:{entry.get('type', 'unknown')}",
                message="Possible hardcoded secret. Move it to an environment "
                        "variable or a secret store.",
                file=filename,
                line=entry.get("line_number"),
            ))
    return findings
