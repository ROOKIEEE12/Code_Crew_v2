"""SARIF 2.1.0 output: findings where developers already look.

GitHub code scanning turns a SARIF upload into annotations on the pull
request; VS Code's SARIF viewer puts them in the Problems panel. Only
located findings with real content are emitted; a gate's verdict is not a
finding, and is carried in the run's properties instead.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import __version__
from .results import GateReport, Severity

_LEVEL = {
    Severity.CRITICAL: "error",
    Severity.HIGH: "error",
    Severity.MEDIUM: "warning",
    Severity.LOW: "note",
    Severity.INFO: "note",
}


def render_sarif(report: GateReport, workspace: Path) -> dict:
    workspace = workspace.resolve()
    rules: dict[str, dict] = {}
    results = []
    for gate in report.results:
        for finding in gate.findings:
            rule_id = f"{gate.gate_id}/{finding.code}"
            if rule_id not in rules:
                rules[rule_id] = {
                    "id": rule_id,
                    "name": finding.code.replace(":", "-"),
                    "shortDescription": {"text": f"{gate.name}: {finding.code}"},
                    "properties": {"gate": gate.gate_id,
                                   "blocking": gate.blocking},
                }
            result = {
                "ruleId": rule_id,
                "level": _LEVEL[finding.severity],
                "message": {"text": finding.message or finding.code},
                "properties": {"severity": finding.severity.value,
                               "gateStatus": gate.status.value},
            }
            uri = _relative_uri(finding.file, workspace)
            if uri:
                location = {"physicalLocation": {
                    "artifactLocation": {"uri": uri, "uriBaseId": "%SRCROOT%"}}}
                if finding.line:
                    location["physicalLocation"]["region"] = {"startLine": finding.line}
                result["locations"] = [location]
            results.append(result)

    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "CodeCrew",
                "version": __version__,
                "informationUri": "https://github.com/ROOKIEEE12/Code_Crew_v2",
                "rules": list(rules.values()),
            }},
            "originalUriBaseIds": {"%SRCROOT%": {"uri": workspace.as_uri() + "/"}},
            "results": results,
            "properties": {
                "verdict": report.verdict(),
                "gates": {g.gate_id: g.status.value for g in report.results},
            },
        }],
    }


def write_sarif(report: GateReport, workspace: Path, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(render_sarif(report, workspace), indent=2),
                    encoding="utf-8")


def _relative_uri(raw: str | None, workspace: Path) -> str | None:
    if not raw:
        return None
    text = raw.replace("\\", "/")
    path = Path(text)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(workspace).as_posix()
        except ValueError:
            return None
    return text.removeprefix("./")
