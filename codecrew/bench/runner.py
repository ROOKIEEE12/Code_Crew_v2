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
    mode: str = "single"
    #: G4 score of the workspace's own tests against its own code. This is
    #: the number Phase 2's hypothesis is about: do independently written
    #: tests detect more injected bugs than self-written ones?
    mutation_score: float | None = None
    branch_coverage: float | None = None
    #: Whether the workspace's own tests passed (G2). In multi mode a failure
    #: here is informative: implementer and test author disagreed, which
    #: means the spec was ambiguous or one of them was wrong.
    own_tests_passed: bool | None = None

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
    #: Means are over tasks where the gate actually produced a score.
    mean_mutation_score: float | None = None
    mean_branch_coverage: float | None = None
    own_tests_passed: int = 0


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


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
        mean_mutation_score=_mean(
            [o.mutation_score for o in outcomes if o.mutation_score is not None]),
        mean_branch_coverage=_mean(
            [o.branch_coverage for o in outcomes if o.branch_coverage is not None]),
        own_tests_passed=sum(1 for o in outcomes if o.own_tests_passed),
    )


class BenchRunner:
    """Generates, verifies and hidden-tests each task in its own workspace."""

    def __init__(
        self,
        llm_config: LLMConfig,
        gate_config: GateConfig | None = None,
        out_dir: Path | None = None,
        mode: str = "single",
    ) -> None:
        self.llm_config = llm_config
        self.gate_config = gate_config or GateConfig()
        self.out_dir = (out_dir or Path("bench_runs")).resolve()
        self.mode = mode

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
        # The spec is auto-approved here: a benchmark cannot pause for a
        # human, and the approval checkpoint is evaluated separately.
        result = pipeline.build(task.requirement, workspace,
                                mode=self.mode, clean=False)
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
            mode=self.mode,
            **_gate_metrics(result),
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


def _gate_metrics(result: RunResult) -> dict:
    """Pull the comparable numbers out of the gate report, if there is one."""
    empty = {"mutation_score": None, "branch_coverage": None,
             "own_tests_passed": None}
    if result.report is None:
        return empty
    by_id = {r.gate_id: r for r in result.report.results}

    def score(gate_id: str) -> float | None:
        gate = by_id.get(gate_id)
        return gate.score if gate is not None else None

    g2 = by_id.get("G2")
    return {
        "mutation_score": score("G4"),
        "branch_coverage": score("G3"),
        "own_tests_passed": None if g2 is None else g2.status.value == "PASS",
    }


def compare_results(baseline: Path, candidate: Path) -> str:
    """Side-by-side of two ``results.json`` files, task by task."""
    def load(path: Path) -> dict:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "outcomes" not in data or "summary" not in data:
            raise ValueError(f"{path} is not a benchmark results file")
        return data

    a, b = load(baseline), load(candidate)
    label_a = a.get("meta", {}).get("mode", "baseline")
    label_b = b.get("meta", {}).get("mode", "candidate")
    if label_a == label_b:
        label_a, label_b = f"{label_a} (A)", f"{label_b} (B)"

    by_task_a = {o["task_id"]: o for o in a["outcomes"]}
    by_task_b = {o["task_id"]: o for o in b["outcomes"]}
    shared = [t for t in by_task_a if t in by_task_b]

    def mut(o: dict) -> str:
        return "-" if o.get("mutation_score") is None else f"{o['mutation_score']:.0f}%"

    rule = "=" * 78
    lines = [rule, f"COMPARISON   A = {label_a}   B = {label_b}", rule,
             f"{'task':16}{'A outcome':19}{'A mut':>7}   {'B outcome':19}{'B mut':>7}",
             "-" * 78]
    for task in shared:
        oa, ob = by_task_a[task], by_task_b[task]
        lines.append(
            f"{task:16}{oa['classification']:19}{mut(oa):>7}   "
            f"{ob['classification']:19}{mut(ob):>7}"
        )
    lines.append("-" * 78)

    sa, sb = a["summary"], b["summary"]

    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.0%}"

    def num(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1f}%"

    rows = [
        ("tasks compared", str(len(shared)), str(len(shared))),
        ("built", f"{sa['built']}/{sa['tasks']}", f"{sb['built']}/{sb['tasks']}"),
        ("hidden tests passed", pct(sa["hidden_pass_rate"]), pct(sb["hidden_pass_rate"])),
        ("own tests green", str(sa.get("own_tests_passed", "n/a")),
         str(sb.get("own_tests_passed", "n/a"))),
        ("mean mutation score", num(sa.get("mean_mutation_score")),
         num(sb.get("mean_mutation_score"))),
        ("mean branch coverage", num(sa.get("mean_branch_coverage")),
         num(sb.get("mean_branch_coverage"))),
        ("FALSE CONFIDENCE", str(sa["false_confidence"]), str(sb["false_confidence"])),
        ("false-confidence rate", pct(sa["false_confidence_rate"]),
         pct(sb["false_confidence_rate"])),
        ("caught by gates", str(sa["caught"]), str(sb["caught"])),
        ("false alarms", str(sa["false_alarm"]), str(sb["false_alarm"])),
        ("tokens", str(sa["total_tokens"]), str(sb["total_tokens"])),
    ]
    lines.append(f"{'metric':26}{label_a:>24}{label_b:>24}")
    for name, left, right in rows:
        lines.append(f"{name:26}{left:>24}{right:>24}")
    lines.append(rule)
    lines.append("n = %d tasks, one run each. Differences of one task are within "
                 "run-to-run noise." % len(shared))
    return "\n".join(lines)


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
        f"own tests green       : {summary.own_tests_passed}/{summary.built}",
        "mean mutation score   : "
        + ("n/a" if summary.mean_mutation_score is None
           else f"{summary.mean_mutation_score:.1f}%"),
        "mean branch coverage  : "
        + ("n/a" if summary.mean_branch_coverage is None
           else f"{summary.mean_branch_coverage:.1f}%"),
        f"tokens used           : {summary.total_tokens}",
        rule,
    ]
    return "\n".join(lines)
