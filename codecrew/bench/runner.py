"""Runs the pipeline over the benchmark and scores it against hidden tests.

The headline metric is **false confidence**: the pipeline said the code was
fine, and the hidden tests say it was not. Everything else (build rate,
hidden pass rate) is context for that one number, because minimising it is
the project's actual goal.

Outcomes form a confusion matrix between what the pipeline *claimed* and
what the hidden tests *show*:

=================  ===============  ==============================
claimed ok         hidden passes    ``true_pass``
claimed ok         hidden fails     ``FALSE_CONFIDENCE``  (the bad one)
claimed not ok     hidden fails     ``caught``            (gates worked)
claimed not ok     hidden passes    ``false_alarm``       (too strict)
=================  ===============  ==============================
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import WORK_DIR_NAME, GateConfig
from ..llm import LLMConfig
from ..pipeline import Pipeline, RunResult
from ..sandbox import get_sandbox
from .tasks import Task

_HIDDEN_RELATIVE = f"{WORK_DIR_NAME}/hidden/test_hidden.py"

TRUE_PASS = "true_pass"
FALSE_CONFIDENCE = "FALSE_CONFIDENCE"
CAUGHT = "caught"
FALSE_ALARM = "false_alarm"
BUILD_FAILED = "build_failed"


@dataclass
class TaskOutcome:
    task_id: str
    tier: str
    built: bool
    verdict: str
    #: The pipeline's own claim: nothing blocking failed.
    claimed_ok: bool
    #: ``None`` when nothing was built to test.
    hidden_passed: bool | None
    hidden_detail: str = ""
    duration_s: float = 0.0
    total_tokens: int = 0
    llm_calls: int = 0
    agent_attempts: int = 0
    contract_rejections: int = 0
    failure: str | None = None

    @property
    def classification(self) -> str:
        if not self.built or self.hidden_passed is None:
            return BUILD_FAILED
        if self.claimed_ok:
            return TRUE_PASS if self.hidden_passed else FALSE_CONFIDENCE
        return FALSE_ALARM if self.hidden_passed else CAUGHT


@dataclass
class BenchSummary:
    tasks: int
    built: int
    hidden_passed: int
    claimed_ok: int
    true_pass: int
    false_confidence: int
    caught: int
    false_alarm: int
    build_failed: int
    #: false_confidence / claimed_ok. The metric the project minimises.
    false_confidence_rate: float | None
    hidden_pass_rate: float
    total_tokens: int
    by_tier: dict[str, dict[str, int]] = field(default_factory=dict)


def summarize(outcomes: list[TaskOutcome]) -> BenchSummary:
    counts = {k: 0 for k in
              (TRUE_PASS, FALSE_CONFIDENCE, CAUGHT, FALSE_ALARM, BUILD_FAILED)}
    by_tier: dict[str, dict[str, int]] = {}
    for outcome in outcomes:
        kind = outcome.classification
        counts[kind] += 1
        tier = by_tier.setdefault(outcome.tier, {"tasks": 0, "hidden_passed": 0})
        tier["tasks"] += 1
        tier["hidden_passed"] += 1 if outcome.hidden_passed else 0

    claimed = sum(1 for o in outcomes if o.built and o.claimed_ok)
    total = len(outcomes)
    passed = sum(1 for o in outcomes if o.hidden_passed)
    return BenchSummary(
        tasks=total,
        built=sum(1 for o in outcomes if o.built),
        hidden_passed=passed,
        claimed_ok=claimed,
        true_pass=counts[TRUE_PASS],
        false_confidence=counts[FALSE_CONFIDENCE],
        caught=counts[CAUGHT],
        false_alarm=counts[FALSE_ALARM],
        build_failed=counts[BUILD_FAILED],
        false_confidence_rate=(
            counts[FALSE_CONFIDENCE] / claimed if claimed else None
        ),
        hidden_pass_rate=passed / total if total else 0.0,
        total_tokens=sum(o.total_tokens for o in outcomes),
        by_tier=by_tier,
    )


class BenchRunner:
    """Generates, verifies and hidden-tests each task in its own workspace."""

    def __init__(
        self,
        llm_config: LLMConfig,
        gate_config: GateConfig | None = None,
        out_dir: Path | None = None,
    ) -> None:
        self.llm_config = llm_config
        self.gate_config = gate_config or GateConfig()
        self.out_dir = (out_dir or Path("bench_runs")).resolve()

    def run(self, tasks: list[Task], *, clean: bool = True) -> list[TaskOutcome]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        outcomes = []
        for index, task in enumerate(tasks, start=1):
            print(f"[{index}/{len(tasks)}] {task.id} ({task.tier}) ...", flush=True)
            outcome = self.run_task(task, clean=clean)
            print(f"      {outcome.classification:17} verdict={outcome.verdict}"
                  f"  hidden={_fmt(outcome.hidden_passed)}", flush=True)
            outcomes.append(outcome)
        return outcomes

    def run_task(self, task: Task, *, clean: bool = True) -> TaskOutcome:
        workspace = self.out_dir / task.id
        if clean and workspace.exists():
            shutil.rmtree(workspace)

        pipeline = Pipeline(self.llm_config, self.gate_config)
        result = pipeline.build(task.requirement, workspace, clean=False)
        return self._score(task, result)

    # -- internals -------------------------------------------------------------

    def _score(self, task: Task, result: RunResult) -> TaskOutcome:
        cost = result.cost or {}
        base = dict(
            task_id=task.id,
            tier=task.tier,
            verdict=result.verdict,
            claimed_ok=result.passed,
            duration_s=round(result.duration_s, 1),
            total_tokens=cost.get("total_tokens", 0),
            llm_calls=cost.get("llm_calls", 0),
            agent_attempts=result.agent_attempts,
            contract_rejections=len(result.contract_rejections),
            failure=result.failure,
        )
        if result.submission is None:
            return TaskOutcome(built=False, hidden_passed=None, **base)

        passed, detail = self._run_hidden(task, result.workspace)
        return TaskOutcome(built=True, hidden_passed=passed,
                           hidden_detail=detail, **base)

    def _run_hidden(self, task: Task, workspace: Path) -> tuple[bool, str]:
        """Run the hidden tests against the generated workspace.

        They are written *after* generation, under the scaffolding
        directory the gates exclude, so the agent can neither see them nor
        have them picked up by its own test discovery.
        """
        target = workspace / _HIDDEN_RELATIVE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(task.hidden_tests.lstrip("\n"), encoding="utf-8")

        sandbox = get_sandbox(workspace, self.gate_config.sandbox)
        sandbox.ensure()
        run = sandbox.run(
            ["python", "-m", "pytest", _HIDDEN_RELATIVE, "-q",
             "-p", "no:cacheprovider", "--no-header"],
            timeout=self.gate_config.thresholds.command_timeout_s,
        )
        return run.ok, _last_line(run.output())


def _last_line(output: str) -> str:
    lines = [line for line in output.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _fmt(value: bool | None) -> str:
    return "n/a" if value is None else ("pass" if value else "FAIL")


def write_results(
    outcomes: list[TaskOutcome], summary: BenchSummary, path: Path,
    *, meta: dict | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta or {},
        "summary": asdict(summary),
        "outcomes": [
            {**asdict(o), "classification": o.classification} for o in outcomes
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def render_summary(outcomes: list[TaskOutcome], summary: BenchSummary) -> str:
    rule = "=" * 74
    lines = [rule, "CODECREW BENCHMARK", rule,
             f"{'task':16}{'tier':10}{'verdict':20}{'hidden':8}outcome",
             "-" * 74]
    for o in outcomes:
        lines.append(
            f"{o.task_id:16}{o.tier:10}{o.verdict:20}"
            f"{_fmt(o.hidden_passed):8}{o.classification}"
        )
    rate = summary.false_confidence_rate
    lines += [
        "-" * 74,
        f"built                 : {summary.built}/{summary.tasks}",
        f"hidden tests passed   : {summary.hidden_passed}/{summary.tasks}"
        f"  ({summary.hidden_pass_rate:.0%})",
        f"pipeline claimed ok   : {summary.claimed_ok}",
        f"  true pass           : {summary.true_pass}",
        f"  FALSE CONFIDENCE    : {summary.false_confidence}",
        f"pipeline rejected     : {summary.caught + summary.false_alarm}",
        f"  caught (correct)    : {summary.caught}",
        f"  false alarm         : {summary.false_alarm}",
        f"build failed          : {summary.build_failed}",
        f"false-confidence rate : "
        + ("n/a (nothing was claimed ok)" if rate is None else f"{rate:.0%}"),
        f"tokens used           : {summary.total_tokens}",
        rule,
    ]
    return "\n".join(lines)
