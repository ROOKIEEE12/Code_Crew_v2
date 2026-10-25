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

import json
import shutil
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable

from .agents import (
    AgentFailure,
    ArchitectAgent,
    CoderAgent,
    CodeSubmission,
    ImplementerAgent,
    ProjectPlan,
    SourceFile,
    SpecAgent,
    Spec,
    TestAuthorAgent,
)
from .badges import FileBadge, Review, compute_badges
from .config import WORK_DIR_NAME, GateConfig
from .docs import render_usage, write_usage
from .gates import GateRunner
from .llm import LLMClient, LLMConfig
from .repair import RepairLoop, RepairStep
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

#: The same, for the architect's project plan.
PlanApprover = Callable[[ProjectPlan], "ProjectPlan | None"]


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
    #: What the reconciliation loop did, in order.
    repairs: list[RepairStep] = field(default_factory=list)
    #: The verdict before any repair, so the loop's effect is measurable
    #: and a repaired result is never presented as a first-try success.
    initial_verdict: str | None = None
    #: Project mode: the architect's plan and one spec per module.
    plan: ProjectPlan | None = None
    specs: dict[str, Spec] = field(default_factory=dict)
    #: Whether a person saw the spec (or plan) before code was written.
    review: Review | None = None
    #: Per-file trust, derived from the final report only.
    badges: list[FileBadge] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.report is None:
            return "FAILED TO BUILD"
        return self.report.verdict()

    @property
    def verdict_note(self) -> str | None:
        """What the verdict does *not* cover, when that is material."""
        if self.report is None or self.review is None:
            return None
        return self.review.caveat()

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
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self.llm_config = llm_config or LLMConfig()
        self.gate_config = gate_config or GateConfig()
        self._tracer = tracer
        #: Observer for live progress (the web UI). Sees every trace event.
        self._on_event = on_event

    def _make_tracer(self, workspace: Path, run_id: str) -> Tracer:
        tracer = self._tracer or Tracer(
            workspace / WORK_DIR_NAME / "traces" / f"{run_id}.jsonl", run_id=run_id
        )
        if self._on_event is not None:
            tracer.subscribe(self._on_event)
        return tracer

    def build(
        self,
        requirement: str,
        workspace: Path,
        *,
        mode: str = "single",
        spec: Spec | None = None,
        plan: ProjectPlan | None = None,
        approve: Approver | None = None,
        approve_plan: PlanApprover | None = None,
        repair_rounds: int = 0,
        clean: bool = False,
        verify: bool = True,
    ) -> RunResult:
        if mode not in ("single", "multi", "project"):
            raise ValueError(
                f"unknown mode {mode!r}; use 'single', 'multi' or 'project'")

        started = time.monotonic()
        workspace = workspace.resolve()
        run_id = time.strftime("%Y%m%d-%H%M%S")

        if clean and workspace.exists():
            # Only ever removes a directory the caller explicitly asked to
            # reset, never a path the agent chose.
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)

        tracer = self._make_tracer(workspace, run_id)
        client = LLMClient(self.llm_config, tracer=tracer)
        tracer.event("run_start", requirement=requirement[:500], mode=mode,
                     provider=client.provider.name, workspace=str(workspace))

        # A spec or plan the caller supplied was written or edited by a
        # person; an approval hook means a person is about to see it.
        reviewed = approve is not None or spec is not None or (
            mode == "project" and plan is not None and approve_plan is not None)

        def finish(**fields) -> RunResult:
            return RunResult(
                requirement=requirement, workspace=workspace, run_id=run_id,
                duration_s=time.monotonic() - started, mode=mode,
                cost=tracer.totals(), provider=client.provider.name, **fields,
            )

        attempts = 0
        rejections: list[str] = []
        specs: dict[str, Spec] = {}

        try:
            if mode == "single":
                result = CoderAgent(client, tracer=tracer).run(
                    requirement=requirement)
                submission = result.output
                attempts += result.attempts
                rejections += result.rejections
            elif mode == "project":
                outcome = self._build_project(
                    client, tracer, requirement, workspace, plan,
                    approve_plan, approve)
                if isinstance(outcome, str):
                    tracer.event("run_end", outcome="plan_rejected", reason=outcome)
                    return finish(submission=None, report=None, failure=outcome,
                                  spec=None, agent_attempts=attempts,
                                  contract_rejections=rejections)
                submission, specs, plan, attempts, rejections = outcome
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
        repairs: list[RepairStep] = []
        initial_verdict = None
        if verify:
            sandbox = get_sandbox(workspace, self.gate_config.sandbox,
                                  project=_project_of(workspace))
            report = self._verify(workspace, sandbox, tracer, run_id)
            initial_verdict = report.verdict()

            # Reconciliation needs a spec to arbitrate against, so it only
            # runs when there is one. A project has one per module; the
            # loop works on the module the failing findings point at.
            if repair_rounds > 0 and not report.passed and (spec or specs):
                owner = None
                spec_path = None
                target = spec
                if specs:
                    owner = owner_module(report, list(specs))
                    target = specs[owner]
                    spec_path = workspace / WORK_DIR_NAME / "specs" / f"{owner}.json"
                    tracer.event("repair_owner", module=owner)
                loop = RepairLoop(
                    client, tracer, workspace,
                    lambda label: self._verify(
                        workspace, sandbox, tracer, f"{run_id}-{label}"),
                    approve=approve, max_rounds=repair_rounds,
                    spec_path=spec_path,
                )
                repaired = loop.run(target, submission, report)
                submission, report = repaired.submission, repaired.report
                if owner is not None:
                    specs[owner] = repaired.spec
                else:
                    spec = repaired.spec
                repairs = repaired.steps
                attempts += repaired.attempts
                rejections += repaired.rejections

        spec_list = list(specs.values()) if specs else ([spec] if spec else [])
        review = None
        if spec_list:
            review = Review(reviewed=reviewed, assumptions=sum(
                len(s.assumptions) for s in spec_list))
            review.write(workspace)
        badges = self._finalise(workspace, report, spec_list, review, plan, tracer)

        tracer.event("run_end", outcome=report.verdict() if report else "not_verified")
        return finish(
            submission=submission, report=report, spec=spec,
            agent_attempts=attempts, contract_rejections=rejections,
            repairs=repairs, initial_verdict=initial_verdict,
            plan=plan, specs=specs, review=review, badges=badges,
        )

    def repair(
        self,
        workspace: Path,
        *,
        rounds: int = 2,
        approve: Approver | None = None,
    ) -> RunResult:
        """Run the gates and the reconciliation loop on an existing workspace.

        Generation is not repeated, so this isolates the loop's effect: the
        same artefacts, before and after. It is also what a user wants after
        editing a generated project by hand.
        """
        from .gates.base import is_excluded_part

        started = time.monotonic()
        workspace = workspace.resolve()
        spec_path = workspace / WORK_DIR_NAME / SPEC_FILE
        if not spec_path.exists():
            raise FileNotFoundError(
                f"{spec_path} not found: the loop arbitrates against a spec, "
                f"so only workspaces built in multi mode can be repaired")
        spec = Spec.parse(spec_path.read_text(encoding="utf-8"))

        files = []
        for path in sorted(workspace.rglob("*.py")):
            rel = path.relative_to(workspace)
            if any(is_excluded_part(part) for part in rel.parts):
                continue
            if rel.name == "conftest.py":
                continue
            files.append(SourceFile(rel.as_posix(),
                                    path.read_text(encoding="utf-8")))
        submission = CodeSubmission(files=files)

        run_id = time.strftime("%Y%m%d-%H%M%S") + "-repair"
        tracer = self._make_tracer(workspace, run_id)
        client = LLMClient(self.llm_config, tracer=tracer)
        tracer.event("repair_start", workspace=str(workspace), rounds=rounds)

        sandbox = get_sandbox(workspace, self.gate_config.sandbox,
                                  project=_project_of(workspace))
        report = self._verify(workspace, sandbox, tracer, run_id)
        initial = report.verdict()
        repairs: list[RepairStep] = []
        attempts, rejections = 0, []

        if rounds > 0 and not report.passed:
            loop = RepairLoop(
                client, tracer, workspace,
                lambda label: self._verify(
                    workspace, sandbox, tracer, f"{run_id}-{label}"),
                approve=approve, max_rounds=rounds,
            )
            outcome = loop.run(spec, submission, report)
            submission, spec, report = (
                outcome.submission, outcome.spec, outcome.report)
            repairs = outcome.steps
            attempts, rejections = outcome.attempts, outcome.rejections

        # The review status belongs to the original build; a repair does not
        # make an unread spec read.
        previous = Review.load(workspace)
        review = Review(reviewed=bool(previous and previous.reviewed)
                        or approve is not None,
                        assumptions=len(spec.assumptions))
        review.write(workspace)
        badges = self._finalise(workspace, report, [spec], review, None, tracer)

        tracer.event("run_end", outcome=report.verdict())
        return RunResult(
            requirement="(repair of an existing workspace)",
            workspace=workspace, run_id=run_id, submission=submission,
            report=report, duration_s=time.monotonic() - started,
            mode="multi", spec=spec, agent_attempts=attempts,
            contract_rejections=rejections, cost=tracer.totals(),
            provider=client.provider.name, repairs=repairs,
            initial_verdict=initial, review=review, badges=badges,
        )

    # -- project stage ---------------------------------------------------------

    def _build_project(
        self,
        client: LLMClient,
        tracer: Tracer,
        requirement: str,
        workspace: Path,
        plan: ProjectPlan | None,
        approve_plan: PlanApprover | None,
        approve: Approver | None,
    ):
        """Architect -> plan approval -> per-module spec, implementation, tests.

        Modules are built dependencies-first, and a module's spec is handed
        the *signatures* of what it may import, taken from the already-built
        specs rather than from the model, so an interface cannot drift
        between the module that offers it and the one that uses it.

        Returns ``(submission, specs, plan, attempts, rejections)``, or a
        string explaining why the run stopped before any code was written.
        """
        attempts = 0
        rejections: list[str] = []

        if plan is None:
            result = ArchitectAgent(client, tracer=tracer).run(
                requirement=requirement)
            plan = result.output
            attempts += result.attempts
            rejections += result.rejections
        else:
            tracer.event("plan_supplied", modules=[m.name for m in plan.modules])

        if approve_plan is not None:
            approved_plan = approve_plan(plan)
            if approved_plan is None:
                tracer.event("plan_decision", decision="rejected")
                return "the project plan was rejected at the approval checkpoint"
            plan = approved_plan
            tracer.event("plan_decision", decision="approved")

        work = workspace / WORK_DIR_NAME
        (work / "specs").mkdir(parents=True, exist_ok=True)
        (work / "plan.json").write_text(plan.to_json(), encoding="utf-8")
        if plan.dependencies:
            (workspace / "requirements.txt").write_text(
                "\n".join(d.name for d in plan.dependencies) + "\n",
                encoding="utf-8")

        specs: dict[str, Spec] = {}
        files: list[SourceFile] = []
        for module in plan.build_order():
            uses = [f"{dep}.{fn.signature}"
                    for dep in module.depends_on
                    for fn in specs[dep].functions]
            module_requirement = (
                f"{module.requirement}\n\nThe module must be named "
                f"`{module.name}` (the file `{module.name}.py` at the "
                f"workspace root)."
            )
            if uses:
                module_requirement += (
                    "\n\nIt may import these already-built modules, and must "
                    "call them rather than re-implement them:\n"
                    + "\n".join(f"- {u}" for u in uses))

            spec_result = SpecAgent(client, tracer=tracer).run(
                requirement=module_requirement)
            spec = spec_result.output
            attempts += spec_result.attempts
            rejections += spec_result.rejections
            if spec.module != module.name:
                # The plan, not the spec agent, owns module names: other
                # modules already import this one by the planned name.
                tracer.event("spec_module_overridden", planned=module.name,
                             got=spec.module)
                spec = Spec.from_dict({**spec.to_dict(), "module": module.name})
            spec = replace(spec, uses=uses)

            if approve is not None:
                approved = approve(spec)
                if approved is None:
                    tracer.event("spec_decision", module=module.name,
                                 decision="rejected")
                    return (f"the specification for '{module.name}' was "
                            f"rejected at the approval checkpoint")
                spec = approved
            specs[module.name] = spec
            (work / "specs" / f"{module.name}.json").write_text(
                spec.to_json(), encoding="utf-8")

            impl = ImplementerAgent(client, tracer=tracer).run(spec=spec)
            tests = TestAuthorAgent(client, tracer=tracer).run(spec=spec)
            attempts += impl.attempts + tests.attempts
            rejections += impl.rejections + tests.rejections
            files += [*impl.output.files, *tests.output.files]

        paths = [f.path for f in files]
        duplicates = sorted({p for p in paths if paths.count(p) > 1})
        if duplicates:
            raise AgentFailure("pipeline", 1,
                               [f"modules produced colliding paths: {duplicates}"])

        submission = CodeSubmission(files=files, notes=plan.summary)
        return submission, specs, plan, attempts, rejections

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

    def _finalise(
        self,
        workspace: Path,
        report: GateReport | None,
        specs: list[Spec],
        review: Review | None,
        plan: ProjectPlan | None,
        tracer: Tracer,
    ) -> list[FileBadge]:
        """Badges and the usage guide, both derived from the final report."""
        if report is None:
            return []
        badges = compute_badges(report, workspace)
        (workspace / WORK_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (workspace / WORK_DIR_NAME / "badges.json").write_text(
            json.dumps([asdict(b) for b in badges], indent=2), encoding="utf-8")
        tracer.event("badges", files={b.path: b.badge for b in badges})
        if specs:
            write_usage(workspace, render_usage(
                specs, report, badges, review=review, plan=plan))
        return badges

    def _verify(
        self, workspace: Path, sandbox: Sandbox, tracer: Tracer, run_id: str
    ) -> GateReport:
        sandbox.ensure()
        runner = GateRunner(
            workspace, self.gate_config, sandbox=sandbox,
            progress=lambda event: tracer.event("gate_progress", **event))
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


def owner_module(report: GateReport, modules: list[str]) -> str:
    """The module most implicated by a failing report.

    Findings carry file paths (``tests/test_stats.py``, an absolute path from
    ruff, ``stats.py``). The first blocked finding whose file belongs to a
    known module names the owner; with no such pointer, the first module
    built is used, since it is the one the others depend on.
    """
    for result in report.results:
        if not result.blocked:
            continue
        for finding in result.findings:
            if not finding.file:
                continue
            stem = Path(finding.file.replace("\\", "/")).name
            stem = stem.removeprefix("test_").removesuffix(".py")
            if stem in modules:
                return stem
    return modules[0]


def _project_of(workspace: Path):
    """The workspace as a project (layout, declared dependencies), if readable."""
    from .project import Project, ProjectConfigError
    try:
        return Project.detect(workspace)
    except ProjectConfigError:
        return None


def _is_within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True
