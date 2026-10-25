"""Human-readable rendering of a gate report.

This is the product surface, not a debug dump. Two rules it must keep:

* every claim traces to an evidence file on disk;
* what was *not* verified is as prominent as what passed, because a skipped
  gate silently rendered as green is exactly the false confidence this
  project exists to remove.
"""

from __future__ import annotations

from .results import GateReport, Severity, Status

_MARK = {
    Status.PASS: "[PASS]",
    Status.FAIL: "[FAIL]",
    Status.SKIP: "[SKIP]",
    Status.ERROR: "[ERR ]",
}

_WIDTH = 74


def render(report: GateReport, *, show_findings: bool = True,
           max_findings: int = 8, isolation: str | None = None) -> str:
    lines: list[str] = []
    rule = "=" * _WIDTH

    lines += [rule, "CODECREW VERIFICATION REPORT", rule]
    lines += [
        f"workspace : {report.workspace}",
        f"run id    : {report.run_id}",
        f"sandbox   : {report.sandbox}",
        f"duration  : {report.duration_s:.1f}s",
        "",
        f"VERDICT   : {report.verdict()}",
        "",
        "-" * _WIDTH,
        "GATES",
        "-" * _WIDTH,
    ]

    for result in report.results:
        mark = _MARK[result.status]
        flag = "" if result.blocking else "  (non-blocking)"
        lines.append(f"{mark} {result.gate_id}  {result.name}: {result.summary}{flag}")
        if result.note:
            lines.append(f"       note: {result.note}")
        if result.evidence:
            lines.append(f"       evidence: {result.evidence}")

    if show_findings:
        findings_section = _findings(report, max_findings)
        if findings_section:
            lines += ["", "-" * _WIDTH, "FINDINGS", "-" * _WIDTH, *findings_section]

    not_verified = _not_verified(report)
    lines += ["", "-" * _WIDTH, "WHAT WAS NOT VERIFIED", "-" * _WIDTH]
    lines += not_verified or ["  Nothing: every gate ran and reported a result."]

    if isolation:
        lines += ["", "-" * _WIDTH, "EXECUTION ISOLATION", "-" * _WIDTH,
                  "  " + isolation]

    counts = report.counts()
    lines += [
        "",
        rule,
        f"pass {counts['PASS']}   fail {counts['FAIL']}   "
        f"skip {counts['SKIP']}   error {counts['ERROR']}",
        rule,
    ]
    return "\n".join(lines)


def render_run(result, *, isolation: str | None = None) -> str:
    """Render a full pipeline run: what was built, then how it was judged.

    The agent's self-assessment is printed under an explicit "unverified
    claim" heading. It is often useful, but it is an opinion from the thing
    being judged, and the layout must never let it read as a result.
    """
    lines = ["=" * _WIDTH, "CODECREW RUN", "=" * _WIDTH]
    lines += [
        f"requirement : {_truncate(result.requirement, 56)}",
        f"workspace   : {result.workspace}",
        f"provider    : {result.provider or 'n/a'}",
        f"mode        : {getattr(result, 'mode', 'single')}",
        f"duration    : {result.duration_s:.1f}s",
    ]
    if result.cost:
        lines.append(
            f"model cost  : {result.cost.get('llm_calls', 0)} call(s), "
            f"{result.cost.get('total_tokens', 0)} token(s), "
            f"{result.cost.get('retries', 0)} retry/retries"
        )
    lines.append(f"agent       : {result.agent_attempts} attempt(s)")
    if result.contract_rejections:
        lines.append(f"              {len(result.contract_rejections)} contract "
                     f"rejection(s) repaired:")
        for rejection in result.contract_rejections:
            lines.append(f"                - {_truncate(rejection, 60)}")

    if result.failure:
        lines += ["", f"VERDICT     : {result.verdict}", "",
                  "-" * _WIDTH, "FAILURE", "-" * _WIDTH]
        for chunk in _wrap(result.failure, _WIDTH - 2):
            lines.append("  " + chunk)
        return "\n".join(lines + ["", "=" * _WIDTH])

    plan = getattr(result, "plan", None)
    if plan is not None:
        lines += ["", "-" * _WIDTH, "PROJECT PLAN", "-" * _WIDTH]
        lines += ["  " + line for line in plan.render().splitlines()]
    for name, module_spec in (getattr(result, "specs", None) or {}).items():
        lines += ["", "-" * _WIDTH, f"SPECIFICATION: {name}", "-" * _WIDTH]
        lines += ["  " + line for line in module_spec.render().splitlines()]

    spec = getattr(result, "spec", None)
    if spec is not None:
        lines += ["", "-" * _WIDTH, "APPROVED SPECIFICATION", "-" * _WIDTH]
        lines += ["  " + line for line in spec.render().splitlines()]

    if result.submission:
        lines += ["", "-" * _WIDTH, "FILES WRITTEN", "-" * _WIDTH]
        for file in result.submission.files:
            tag = "test" if file.is_test else "source"
            lines.append(f"  [{tag:6}] {file.path}  "
                         f"({len(file.content.splitlines())} lines)")
        if result.submission.notes:
            lines += ["", "  approach: " + "\n            ".join(
                _wrap(result.submission.notes, _WIDTH - 14))]

    repairs = getattr(result, "repairs", [])
    if repairs:
        initial = getattr(result, "initial_verdict", None)
        lines += ["", "-" * _WIDTH, "RECONCILIATION LOG", "-" * _WIDTH,
                  f"  before repair : {initial}",
                  f"  after repair  : {result.verdict}"]
        for step in repairs:
            verdict = step.verdict or "no diagnosis"
            lines.append(f"  round {step.round}: {verdict:12} -> {step.outcome}")
            if step.instruction:
                for chunk in _wrap(step.instruction, _WIDTH - 12):
                    lines.append("      " + chunk)
            if step.note:
                lines.append(f"      ({step.note})")
        lines.append("  A repaired result is shown with its pre-repair verdict "
                     "so it is never mistaken for a first-try success.")

    if result.report is not None:
        lines += ["", render(result.report, isolation=isolation)]

    note = getattr(result, "verdict_note", None)
    if note:
        lines += ["", f"NOTE: {result.verdict} {note}.",
                  "  The gates check the code against the spec; nobody checked "
                  "the spec",
                  "  against what you meant. Read its assumptions."]

    badges = getattr(result, "badges", None)
    if badges:
        lines += ["", "-" * _WIDTH, "TRUST PER FILE", "-" * _WIDTH]
        for badge in badges:
            lines.append(f"  {badge.badge:20} {badge.path}")
            for reason in badge.reasons[:3]:
                lines.append(f"      - {_truncate(reason, _WIDTH - 10)}")

    claim = result.unverified_claim()
    if claim:
        lines += ["", "-" * _WIDTH,
                  "UNVERIFIED CLAIM (the agent's own opinion, not a result)",
                  "-" * _WIDTH]
        for chunk in _wrap(claim, _WIDTH - 2):
            lines.append("  " + chunk)

    return "\n".join(lines)


_MD_MARK = {Status.PASS: "✅ PASS", Status.FAIL: "❌ FAIL",
            Status.SKIP: "⚪ SKIP", Status.ERROR: "⚠️ ERROR"}


def render_markdown(report: GateReport, badges=None, *, note: str | None = None) -> str:
    """The report as GitHub-flavoured Markdown (a CI job summary, a PR)."""
    lines = [f"## CodeCrew: {report.verdict()}", ""]
    if note:
        lines += [f"> {report.verdict()} {note}.", ""]
    lines += ["| Gate | Result | Summary |", "|---|---|---|"]
    for result in report.results:
        flag = "" if result.blocking else " (advisory)"
        summary = result.summary + (f" ({result.note})" if result.note else "")
        lines.append(f"| {result.gate_id} {result.name}{flag} | "
                     f"{_MD_MARK[result.status]} | {_md_cell(summary)} |")
    not_verified = _not_verified(report)
    lines += ["", "**What was not verified**", ""]
    lines += [f"- {line.strip()}" for line in not_verified] or [
        "- Nothing: every gate ran and reported a result."]
    if badges:
        lines += ["", "**Trust per file**", "", "| File | Badge |", "|---|---|"]
        lines += [f"| `{b.path}` | {b.badge} |" for b in badges]
    serious = [(r, f) for r in report.results for f in r.findings
               if f.severity.rank >= Severity.MEDIUM.rank]
    if serious:
        lines += ["", "<details><summary>Findings "
                  f"({len(serious)})</summary>", ""]
        for result, finding in serious[:40]:
            lines.append(f"- **{result.gate_id}** `{finding.location()}` "
                         f"{finding.code}: {_md_cell(finding.message)}")
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def _md_cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _truncate(text: str, limit: int) -> str:
    single_line = " ".join(text.split())
    if len(single_line) <= limit:
        return single_line
    return single_line[: limit - 3] + "..."


def _findings(report: GateReport, max_findings: int) -> list[str]:
    lines: list[str] = []
    for result in report.results:
        if not result.findings:
            continue
        shown = sorted(result.findings, key=lambda f: -f.severity.rank)
        lines.append(f"  {result.gate_id} {result.name}:")
        for finding in shown[:max_findings]:
            lines.append(
                f"    [{finding.severity.value:8}] {finding.location()}  "
                f"{finding.code}"
            )
            for chunk in _wrap(finding.message, _WIDTH - 8):
                lines.append(f"        {chunk}")
        if len(shown) > max_findings:
            lines.append(f"    ... and {len(shown) - max_findings} more "
                         f"(see {result.evidence or 'evidence directory'})")
    return lines


def _not_verified(report: GateReport) -> list[str]:
    """The honesty section: unresolved gaps, stated plainly."""
    lines: list[str] = []
    for result in report.skipped:
        reason = result.note or "no reason recorded"
        advisory = "" if result.blocking else " (advisory: does not change the verdict)"
        lines.append(f"  {result.gate_id} {result.name} did not run - {reason}{advisory}")
    for result in report.results:
        if result.status is Status.ERROR:
            lines.append(
                f"  {result.gate_id} {result.name} could not complete - "
                f"{result.note or result.summary}"
            )
    low = [
        f for result in report.results for f in result.findings
        if f.severity.rank < Severity.MEDIUM.rank
    ]
    if low:
        lines.append(f"  {len(low)} informational finding(s) were recorded but "
                     f"did not affect the verdict.")
    return lines


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines, current = [], words[0]
    for word in words[1:]:
        if len(current) + len(word) + 1 <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines
