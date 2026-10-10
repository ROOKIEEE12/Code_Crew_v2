"""The reconciliation loop: diagnose which artefact is wrong, fix that one,
and prove the fix did not make anything else worse.

Three properties distinguish it from a generic "retry until green" loop,
each answering a failure mode that loop is known for:

1. **It asks who is wrong, not how to pass.** The Diagnostician rules, against
   the spec, whether code, tests or spec is at fault. Bending correct code
   to satisfy a wrong test is the classic automated-debugging failure.
2. **Every gate re-runs after every patch.** Security has been measured to
   *degrade* across LLM refinement iterations, so a patch that fixes the
   failing test but introduces an ``eval`` must not survive.
3. **A patch is kept only on evidence of progress, and reverted otherwise.**
   Not "the model said it fixed it": the gate report must have more passing
   gates, and no gate that passed before may fail now.

The ladder is deliberately short (patch, then patch again with the failed
attempts as negative examples, then stop). When it cannot fix the problem it
says so; returning broken code under a green-looking summary would defeat the
purpose of the project. Full regeneration was left off the ladder on purpose:
at temperature 0 it would usually reproduce the same output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .agents import (
    AgentFailure,
    CodeSubmission,
    DiagnosticianAgent,
    PatcherAgent,
    SourceFile,
    Spec,
    SpecRepairAgent,
    TestAuthorAgent,
)
from .agents.contracts import (
    CODE_DEFECT,
    SPEC_DEFECT,
    TEST_DEFECT,
    Diagnosis,
)
from .config import WORK_DIR_NAME
from .llm import LLMClient
from .results import GateReport, Status
from .trace import Tracer

Verifier = Callable[[str], GateReport]
Approver = Callable[[Spec], "Spec | None"]

ACCEPTED = "accepted"
REVERTED = "reverted"
AGENT_FAILED = "agent_failed"
REFUSED = "refused"

_EVIDENCE_TAIL = 2500
_MAX_FINDINGS = 10


@dataclass
class RepairStep:
    round: int
    verdict: str | None
    targets: list[str]
    instruction: str
    outcome: str
    note: str = ""


@dataclass
class RepairOutcome:
    steps: list[RepairStep]
    resolved: bool
    submission: CodeSubmission
    spec: Spec
    report: GateReport
    attempts: int = 0
    rejections: list[str] = field(default_factory=list)


def progress_key(report: GateReport) -> tuple[int, int]:
    """Orderable measure of how far the workspace got. Higher is better.

    Counting *passing* gates rather than failing ones matters: fixing G2 lets
    G3 and G4 run for the first time, which can add new failures. That is
    progress being unmasked, not regression, and a failure-count metric
    would revert exactly the patches that worked.
    """
    passed = sum(1 for r in report.results
                 if r.status is Status.PASS and r.blocking)
    failing_tests = sum(1 for r in report.results for f in r.findings
                        if f.code == "test-failure")
    return (passed, -failing_tests)


def _passing(report: GateReport) -> set[str]:
    return {r.gate_id for r in report.results
            if r.status is Status.PASS and r.blocking}


def describe_failure(report: GateReport, workspace: Path) -> str:
    """The blocked gates, with located findings and a tail of raw evidence."""
    blocks = []
    for result in report.results:
        if not result.blocked:
            continue
        lines = [f"{result.gate_id} {result.name}: {result.status.value} - "
                 f"{result.summary}"]
        if result.note:
            lines.append(f"  note: {result.note}")
        ranked = sorted(result.findings, key=lambda f: -f.severity.rank)
        for finding in ranked[:_MAX_FINDINGS]:
            lines.append(f"  - [{finding.severity.value}] {finding.location()} "
                         f"{finding.code}: {finding.message}")
        if len(ranked) > _MAX_FINDINGS:
            lines.append(f"  ... and {len(ranked) - _MAX_FINDINGS} more")
        if result.evidence:
            path = workspace / result.evidence
            if path.exists():
                text = path.read_text(encoding="utf-8", errors="replace")
                lines.append("  evidence (tail):")
                lines += ["    " + ln for ln in text[-_EVIDENCE_TAIL:].splitlines()]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "(no blocking failures recorded)"


class RepairLoop:
    def __init__(
        self,
        client: LLMClient,
        tracer: Tracer,
        workspace: Path,
        verify: Verifier,
        *,
        approve: Approver | None = None,
        max_rounds: int = 2,
        spec_path: Path | None = None,
    ) -> None:
        self.client = client
        self.tracer = tracer
        self.workspace = workspace.resolve()
        self.verify = verify
        self.approve = approve
        self.max_rounds = max_rounds
        #: Where this spec lives on disk. A project has one spec per module,
        #: so the loop is told which one it is arbitrating against.
        self.spec_path = (spec_path or
                          self.workspace / WORK_DIR_NAME / "spec.json")

    # -- main loop -------------------------------------------------------------

    def run(self, spec: Spec, submission: CodeSubmission,
            report: GateReport) -> RepairOutcome:
        steps: list[RepairStep] = []
        history: list[dict] = []
        attempts = 0
        rejections: list[str] = []

        for round_no in range(1, self.max_rounds + 1):
            if report.passed:
                break
            self.tracer.event("repair_round", round=round_no,
                              blocked=[r.gate_id for r in report.results if r.blocked])
            failure = describe_failure(report, self.workspace)
            files = self._current_files(submission)

            try:
                diag = DiagnosticianAgent(self.client, tracer=self.tracer).run(
                    spec=spec, failure=failure, files=files)
            except AgentFailure as exc:
                steps.append(RepairStep(round_no, None, [], "", AGENT_FAILED,
                                        f"diagnosis failed: {exc}"))
                history.append({"verdict": "?", "instruction": "diagnose",
                                "outcome": "agent could not produce a diagnosis"})
                continue
            attempts += diag.attempts
            rejections += diag.rejections
            diagnosis = diag.output
            self.tracer.event("repair_diagnosis", **diagnosis.to_dict())

            snapshot = self._snapshot(submission, spec)
            before_key, before_pass = progress_key(report), _passing(report)

            try:
                new_submission, new_spec, used = self._apply(
                    diagnosis, spec, submission, failure, history)
            except AgentFailure as exc:
                steps.append(RepairStep(
                    round_no, diagnosis.verdict, list(diagnosis.target_files),
                    diagnosis.fix_instruction, AGENT_FAILED, str(exc)))
                history.append({"verdict": diagnosis.verdict,
                                "instruction": diagnosis.fix_instruction,
                                "outcome": "the patch was rejected by its contract"})
                continue
            except _Refused as exc:
                steps.append(RepairStep(
                    round_no, diagnosis.verdict, [], diagnosis.fix_instruction,
                    REFUSED, str(exc)))
                break
            attempts += used[0]
            rejections += used[1]

            new_report = self.verify(f"repair{round_no}")
            after_key, after_pass = progress_key(new_report), _passing(new_report)
            regressed = sorted(before_pass - after_pass)

            if new_report.passed or (after_key > before_key and not regressed):
                outcome, note = ACCEPTED, f"progress {before_key} -> {after_key}"
                submission, spec, report = new_submission, new_spec, new_report
            else:
                outcome = REVERTED
                note = (f"regressed {', '.join(regressed)}" if regressed
                        else f"no progress ({before_key} -> {after_key})")
                self._restore(snapshot)

            steps.append(RepairStep(
                round_no, diagnosis.verdict, list(diagnosis.target_files),
                diagnosis.fix_instruction, outcome, note))
            history.append({"verdict": diagnosis.verdict,
                            "instruction": diagnosis.fix_instruction,
                            "outcome": f"{outcome}: {note}"})
            self.tracer.event("repair_outcome", round=round_no, outcome=outcome,
                              note=note)

        return RepairOutcome(steps=steps, resolved=report.passed,
                             submission=submission, spec=spec, report=report,
                             attempts=attempts, rejections=rejections)

    # -- applying a diagnosis --------------------------------------------------

    def _apply(self, diagnosis: Diagnosis, spec: Spec, submission: CodeSubmission,
               failure: str, history: list[dict]):
        """Write the fix to disk. Returns (submission, spec, (attempts, rejections))."""
        if diagnosis.verdict in (CODE_DEFECT, TEST_DEFECT):
            current = {f.path: f.content for f in submission.files}
            targets = {p: current[p] for p in diagnosis.target_files}
            removals = max(1, sum(
                1 for line in failure.splitlines() if "test-failure" in line))
            result = PatcherAgent(self.client, tracer=self.tracer).run(
                spec=spec, diagnosis=diagnosis, failure=failure,
                files=targets, history=history, max_test_removals=removals)
            patched = {f.path: f.content for f in result.output.files}
            self._write(patched)
            merged = [SourceFile(f.path, patched.get(f.path, f.content))
                      for f in submission.files]
            return (CodeSubmission(files=merged, notes=submission.notes,
                                   self_assessment=submission.self_assessment),
                    spec, (result.attempts, result.rejections))

        # spec_defect: correct the spec, then re-derive the tests from it,
        # independently, exactly as in the original build. The implementation
        # is left alone; if the corrected rules contradict it, the next
        # round's gates will say so.
        repaired = SpecRepairAgent(self.client, tracer=self.tracer).run(
            spec=spec, diagnosis=diagnosis, failure=failure)
        new_spec = repaired.output
        if self.approve is not None:
            approved = self.approve(new_spec)
            if approved is None:
                raise _Refused("the corrected specification was rejected")
            new_spec = approved

        tests = TestAuthorAgent(self.client, tracer=self.tracer).run(spec=new_spec)
        old_tests = [f.path for f in submission.files if f.is_test]
        for rel in old_tests:
            target = (self.workspace / rel).resolve()
            if _within(target, self.workspace) and target.exists():
                target.unlink()
        new_files = {f.path: f.content for f in tests.output.files}
        self._write(new_files)
        self.spec_path.parent.mkdir(parents=True, exist_ok=True)
        self.spec_path.write_text(new_spec.to_json(), encoding="utf-8")

        kept = [f for f in submission.files if not f.is_test]
        merged = kept + [SourceFile(p, c) for p, c in new_files.items()]
        return (CodeSubmission(files=merged, notes=submission.notes,
                               self_assessment=submission.self_assessment),
                new_spec,
                (repaired.attempts + tests.attempts,
                 repaired.rejections + tests.rejections))

    # -- disk ------------------------------------------------------------------

    def _current_files(self, submission: CodeSubmission) -> dict[str, str]:
        """Read the real on-disk content, not the submission's cached copy."""
        out = {}
        for f in submission.files:
            if not f.is_python:
                continue
            path = self.workspace / f.path
            out[f.path] = (path.read_text(encoding="utf-8")
                           if path.exists() else f.content)
        return out

    def _snapshot(self, submission: CodeSubmission, spec: Spec) -> dict:
        return {
            "submission": submission,
            "spec": spec,
            "files": self._current_files(submission),
            "spec_json": self.spec_path.read_text(encoding="utf-8")
            if self.spec_path.exists() else None,
        }

    def _restore(self, snapshot: dict) -> None:
        """Put the workspace back exactly as it was before the patch."""
        original: CodeSubmission = snapshot["submission"]
        keep = {f.path for f in original.files}
        # Anything a patch created that the snapshot did not have.
        for path in self.workspace.rglob("*.py"):
            rel = path.relative_to(self.workspace).as_posix()
            if rel.startswith(WORK_DIR_NAME + "/") or "__pycache__" in rel:
                continue
            if rel in ("conftest.py",):
                continue
            if rel not in keep and SourceFile(rel, "").is_test:
                path.unlink()
        self._write(snapshot["files"])
        if snapshot["spec_json"] is not None:
            self.spec_path.write_text(snapshot["spec_json"], encoding="utf-8")

    def _write(self, files: dict[str, str]) -> None:
        for rel, content in files.items():
            target = (self.workspace / rel).resolve()
            if not _within(target, self.workspace):
                raise ValueError(f"refusing to write outside the workspace: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")


class _Refused(Exception):
    """A human declined the corrected spec; stop without further changes."""


def _within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


def steps_to_dicts(steps: list[RepairStep]) -> list[dict]:
    return [json.loads(json.dumps(s.__dict__)) for s in steps]
