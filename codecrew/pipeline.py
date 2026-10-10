"""Phase 1 pipeline: requirement -> code -> gates -> verdict.

This is the single-agent baseline wired to the gate layer. One agent writes
the implementation and the tests; the gates then judge the result. No
debug loop yet (Phase 3), and no split between coder and test author
(Phase 2) - those are the comparisons this baseline exists to be measured
against.

The rule the pipeline enforces structurally: the agent's own opinion of its
work is recorded as an *unverified claim* and never contributes to the
verdict. Only the gate report decides.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .agents import AgentFailure, CodeSubmission, CoderAgent
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


@dataclass
class RunResult:
    """Everything one pipeline run produced."""

    requirement: str
    workspace: Path
    run_id: str
    submission: CodeSubmission | None
    report: GateReport | None
    duration_s: float
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
        clean: bool = False,
        verify: bool = True,
    ) -> RunResult:
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
        tracer.event("run_start", requirement=requirement[:500],
                     provider=client.provider.name, workspace=str(workspace))

        agent = CoderAgent(client, tracer=tracer)
        try:
            result = agent.run(requirement=requirement)
        except AgentFailure as exc:
            tracer.event("run_end", outcome="agent_failed", error=str(exc))
            return RunResult(
                requirement=requirement,
                workspace=workspace,
                run_id=run_id,
                submission=None,
                report=None,
                duration_s=time.monotonic() - started,
                failure=str(exc),
                agent_attempts=exc.attempts,
                contract_rejections=exc.rejections,
                cost=tracer.totals(),
                provider=client.provider.name,
            )

        submission = result.output
        self._materialise(workspace, submission)
        tracer.event("files_written", count=len(submission.files),
                     paths=[f.path for f in submission.files])

        report = None
        if verify:
            sandbox = get_sandbox(workspace, self.gate_config.sandbox)
            report = self._verify(workspace, sandbox, tracer, run_id)

        tracer.event("run_end", outcome=report.verdict() if report else "not_verified")
        return RunResult(
            requirement=requirement,
            workspace=workspace,
            run_id=run_id,
            submission=submission,
            report=report,
            duration_s=time.monotonic() - started,
            agent_attempts=result.attempts,
            contract_rejections=result.rejections,
            cost=tracer.totals(),
            provider=client.provider.name,
        )

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
