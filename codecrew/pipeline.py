"""Pipelines: requirement -> code -> gates -> verdict.

Two modes, deliberately kept side by side so they can be compared:

``single``
    One agent writes the implementation *and* its tests, as a conventional
    AI coding assistant does. This is the baseline.

``multi``
    Spec agent -> human approval -> Implementer and Test-Author working
    independently from the approved spec. Neither sees the other's output,
    so the tests cannot have been shaped to fit the code.

Both modes feed the same gate layer, so any difference in the results is
down to the architecture and not to a difference in how it was judged.

The rule enforced structurally in both: an agent's opinion of its own work
is recorded as an *unverified claim* and never contributes to the verdict.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .agents import (
    AgentFailure,
    CoderAgent,
    CodeSubmission,
    ImplementerAgent,
    SpecAgent,
    Spec,
    TestAuthorAgent,
)
from .config import WORK_DIR_NAME, GateConfig
from .gates import GateRunner
from .llm import LLMClient, LLMConfig
from .results import GateReport
from .sandbox import Sandbox, get_sandbox
from .trace import Tracer

#: Written into every generated workspace so the tests can import the module
#: under test, and so pytest's rootdir stops at the workspace.
_CONFTEST = '''\
"""Makes the module under test importable from the workspace root."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
'''

_PYTEST_INI = """\
; Keeps this workspace self-contained.
[pytest]
testpaths = tests
"""

SPEC_FILE = "spec.json"

#: The approval hook. Receives the spec, returns the spec to build from
#: (possibly edited), or ``None`` to reject it and stop.
Approver = Callable[[Spec], "Spec | None"]


@dataclass
class RunResult:
    """Everything one pipeline run produced."""

    requirement: str
    workspace: Path
    run_id: str
    submission: CodeSubmission | None
    report: GateReport | None
    duration_s: float
    mode: str = "single"
    spec: Spec | None = None
    #: Set when the agent never produced contract-valid output.
    failure: str | None = None
    agent_attempts: int = 0
    contract_rejections: list[str] = field(default_factory=list)
    cost: dict = field(default_factory=dict)
    provider: str = ""

    @property
    def verdict(self) -> str:
        if self.report is None:
            return "FAILED TO BUILD"
        return self.report.verdict()

    @property
    def passed(self) -> bool:
        return self.report is not None and self.report.passed

    def unverified_claim(self) -> str | None:
        """The agent's self-assessment, explicitly labelled as unverified.

        Kept visible because it is useful ("I am unsure about the unicode
        handling") but never allowed to look like a result.
        """
        if self.submission and self.submission.self_assessment:
            return self.submission.self_assessment
        return None


class Pipeline:
    """Builds and verifies one requirement."""

    def __init__(
        self,
        llm_config: LLMConfig | None = None,
        gate_config: GateConfig | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.llm_config = llm_config or LLMConfig()
        self.gate_config = gate_config or GateConfig()
        self._tracer = tracer

    def build(
        self,
        requirement: str,
        workspace: Path,
        *,
        mode: str = "single",
        spec: Spec | None = None,
        approve: Approver | None = None,
        clean: bool = False,
        verify: bool = True,
    ) -> RunResult:
        if mode not in ("single", "multi"):
            raise ValueError(f"unknown mode {mode!r}; use 'single' or 'multi'")

        started = time.monotonic()
        workspace = workspace.resolve()
        run_id = time.strftime("%Y%m%d-%H%M%S")

        if clean and workspace.exists():
            # Only ever removes a directory the caller explicitly asked to
            # reset, never a path the agent chose.
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)

        tracer = self._tracer or Tracer(
            workspace / WORK_DIR_NAME / "traces" / f"{run_id}.jsonl", run_id=run_id
        )
        client = LLMClient(self.llm_config, tracer=tracer)
        tracer.event("run_start", requirement=requirement[:500], mode=mode,
                     provider=client.provider.name, workspace=str(workspace))

        def finish(**fields) -> RunResult:
            return RunResult(
                requirement=requirement, workspace=workspace, run_id=run_id,
                duration_s=time.monotonic() - started, mode=mode,
                cost=tracer.totals(), provider=client.provider.name, **fields,
            )

        attempts = 0
        rejections: list[str] = []

        try:
            if mode == "single":
                result = CoderAgent(client, tracer=tracer).run(
                    requirement=requirement)
                submission = result.output
                attempts += result.attempts
                rejections += result.rejections
            else:
                outcome = self._build_multi(
                    client, tracer, requirement, workspace, spec, approve)
                if isinstance(outcome, str):  # a refusal, with the reason
                    tracer.event("run_end", outcome="spec_rejected", reason=outcome)
                    return finish(submission=None, report=None, failure=outcome,
                                  spec=None, agent_attempts=attempts,
                                  contract_rejections=rejections)
                submission, spec, attempts, rejections = outcome
        except AgentFailure as exc:
            tracer.event("run_end", outcome="agent_failed", error=str(exc))
            return finish(
                submission=None, report=None, failure=str(exc), spec=spec,
                agent_attempts=exc.attempts, contract_rejections=exc.rejections,
            )

        self._materialise(workspace, submission)
        tracer.event("files_written", count=len(submission.files),
                     paths=[f.path for f in submission.files])

        report = None
        if verify:
            sandbox = get_sandbox(workspace, self.gate_config.sandbox)
            report = self._verify(workspace, sandbox, tracer, run_id)

        tracer.event("run_end", outcome=report.verdict() if report else "not_verified")
        return finish(
            submission=submission, report=report, spec=spec,
            agent_attempts=attempts, contract_rejections=rejections,
        )

    # -- multi-agent stages ----------------------------------------------------

    def _build_multi(
        self,
        client: LLMClient,
        tracer: Tracer,
        requirement: str,
        workspace: Path,
        spec: Spec | None,
        approve: Approver | None,
    ):
        """Spec -> approval -> independent implementer and test author.

        Returns ``(submission, spec, attempts, rejections)``, or a string
        describing why the run was stopped before any code was written.
        """
        attempts = 0
        rejections: list[str] = []

        if spec is None:
            result = SpecAgent(client, tracer=tracer).run(requirement=requirement)
            spec = result.output
            attempts += result.attempts
            rejections += result.rejections
        else:
            tracer.event("spec_supplied", module=spec.module)

        if approve is not None:
            approved = approve(spec)
            if approved is None:
                tracer.event("spec_decision", decision="rejected")
                return "specification was rejected at the approval checkpoint"
            spec = approved
            tracer.event("spec_decision", decision="approved")

        spec_path = workspace / WORK_DIR_NAME / SPEC_FILE
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(spec.to_json(), encoding="utf-8")

        # Each agent is handed the spec and nothing else. The base class
        # raises if it is given anything more, and records what it received.
        impl = ImplementerAgent(client, tracer=tracer).run(spec=spec)
        tests = TestAuthorAgent(client, tracer=tracer).run(spec=spec)
        attempts += impl.attempts + tests.attempts
        rejections += impl.rejections + tests.rejections

        submission = CodeSubmission(
            files=[*impl.output.files, *tests.output.files],
            notes=impl.output.notes,
            self_assessment=impl.output.self_assessment,
        )
        collisions = {f.path for f in impl.output.files} & \
            {f.path for f in tests.output.files}
        if collisions:  # contracts make this unreachable; belt and braces
            raise AgentFailure("pipeline", 1,
                               [f"path collision: {sorted(collisions)}"])
        return submission, spec, attempts, rejections

    # -- internals -------------------------------------------------------------

    def _materialise(self, workspace: Path, submission: CodeSubmission) -> None:
        """Write the submitted files, re-checking containment at write time.

        The contract already rejected absolute paths and ``..``, but this is
        the step that actually touches the filesystem, so it verifies the
        resolved target again rather than trusting an earlier check.
        """
        for file in submission.files:
            target = (workspace / file.path).resolve()
            if not _is_within(target, workspace):
                raise ValueError(
                    f"refusing to write outside the workspace: {file.path}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file.content, encoding="utf-8")

        # Scaffolding the agent is not asked to produce, so that a correct
        # submission is not failed for an import-path technicality.
        conftest = workspace / "conftest.py"
        if not conftest.exists():
            conftest.write_text(_CONFTEST, encoding="utf-8")
        ini = workspace / "pytest.ini"
        if not ini.exists():
            ini.write_text(_PYTEST_INI, encoding="utf-8")

    def _verify(
        self, workspace: Path, sandbox: Sandbox, tracer: Tracer, run_id: str
    ) -> GateReport:
        sandbox.ensure()
        runner = GateRunner(workspace, self.gate_config, sandbox=sandbox)
        report = runner.run(run_id=run_id)
        for result in report.results:
            tracer.event(
                "gate", gate=result.gate_id, status=result.status.value,
                summary=result.summary, score=result.score,
                evidence=result.evidence, blocking=result.blocking,
            )
        report.write_json(
            workspace / WORK_DIR_NAME / "reports" / f"{report.run_id}.json"
        )
        return report


def _is_within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True
