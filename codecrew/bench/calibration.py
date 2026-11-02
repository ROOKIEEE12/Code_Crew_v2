"""How accurate is the judge? Calibrating the gate layer against known truth.

The evaluation study (ablation.py) measures the AI pipeline, which needs a
model and a quota. This measures the gates themselves, offline and exactly,
because here the right answer is known for every case:

``good``
    The task's reference solution with its hidden tests as the project's
    tests. Correct code, strong tests: the gates should pass it. A rejection
    is a **false alarm**.
``wrong_green``
    A buggy version of the solution, with only the tests that still pass on
    it. The project's own tests are all green, which is exactly what a
    "tests pass" check (and a vibe coder) relies on. The gates should still
    reject it. A pass is **false confidence**, the failure CodeCrew exists
    to prevent. The baseline "tests pass" check gets every one of these
    wrong, by construction.
``right_thin``
    The correct solution with the same thin tests. Informational: whether
    the gates flag tests too weak to vouch for correct code.

Bugs come from two sources, and they are reported apart because one of them
is biased in CodeCrew's favour:

* ``mutant``: changes made by CodeCrew's mutation engine that the hidden
  tests detect (real behavioural bugs). G4 uses the same operators, so G4's
  share of these catches is optimistic; the catch rate *without* G4 is
  reported beside it.
* ``handwritten``: plausible wrong answers written by hand for the hard
  tasks (``KNOWN_WRONG``), independent of the mutation engine.
"""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .. import mutation as engine
from ..config import GateConfig
from ..gates import GateRunner
from ..results import GateReport
from .ablation import wilson
from .tasks import Task

GOOD = "good"
WRONG_GREEN = "wrong_green"
WRONG_VIBE = "wrong_vibe"
RIGHT_THIN = "right_thin"
#: Passing tests kept in the "vibe" case: what a quick happy-path suite holds.
VIBE_TESTS = 2

PYTEST_INI = "[pytest]\ntestpaths = tests\n"

_DESELECT_CONFTEST = '''\
"""Calibration: the tests this project "forgot" (they would expose the bug)."""

DESELECTED = {deselected!r}


def pytest_collection_modifyitems(config, items):
    items[:] = [item for item in items if item.nodeid not in DESELECTED]
'''

_FAILED = re.compile(r"^FAILED (.+?)(?: - .*)?$", re.MULTILINE)


@dataclass
class CaseResult:
    task_id: str
    kind: str                # good | wrong_green | right_thin
    bug_source: str          # "" | mutant | handwritten
    bug: str                 # description of the injected bug
    passed: bool             # the gates' claim
    verdict: str
    gate_statuses: dict[str, str] = field(default_factory=dict)
    #: For wrong_green: the blocking gates that failed, i.e. what caught it.
    caught_by: list[str] = field(default_factory=list)
    kept_tests: int = 0
    dropped_tests: int = 0
    duration_s: float = 0.0


# -- building cases ---------------------------------------------------------------------

def write_project(root: Path, module: str, source: str, tests: str,
                  deselected: list[str] | None = None) -> Path:
    if root.exists():
        shutil.rmtree(root)
    (root / "tests").mkdir(parents=True)
    (root / f"{module}.py").write_text(source.lstrip("\n"), encoding="utf-8")
    (root / "tests" / "test_project.py").write_text(tests.lstrip("\n"), encoding="utf-8")
    (root / "pytest.ini").write_text(PYTEST_INI, encoding="utf-8")
    if deselected:
        (root / "tests" / "conftest.py").write_text(
            _DESELECT_CONFTEST.format(deselected=sorted(deselected)), encoding="utf-8")
    return root


def failing_tests(project: Path, python: str | None = None) -> tuple[list[str], int]:
    """Node ids of the failing tests, and the number collected."""
    python = python or _tool_python()
    run = subprocess.run(
        [python, "-m", "pytest", "tests", "-q", "-rf", "-p", "no:cacheprovider",
         "--no-header"],
        cwd=project, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180)
    failed = _FAILED.findall(run.stdout)
    counts = re.findall(r"(\d+) (passed|failed)", run.stdout)
    total = sum(int(n) for n, _ in counts)
    return failed, total


def collected_tests(project: Path) -> list[str]:
    """Node ids of every test, in collection order."""
    run = subprocess.run(
        [_tool_python(), "-m", "pytest", "tests", "-q", "--collect-only",
         "-p", "no:cacheprovider", "--no-header"],
        cwd=project, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180)
    return [line.strip() for line in run.stdout.splitlines() if "::" in line]


def mutant_bugs(task: Task, scratch: Path, limit: int, seed: int) -> list[tuple[str, str]]:
    """Up to ``limit`` mutants of the reference that some hidden tests catch
    and some do not (so a green-but-wrong project exists)."""
    write_project(scratch, task.module, task.reference, task.hidden_tests)
    sites = engine.collect_sites(scratch, [Path(f"{task.module}.py")])
    random.Random(seed).shuffle(sites)
    bugs = []
    for site in sites[: limit * 6]:
        try:
            source = engine.mutated_source(scratch, site)
        except (SyntaxError, ValueError, IndexError, OSError):
            continue
        probe = write_project(scratch / "probe", task.module, source, task.hidden_tests)
        failed, total = failing_tests(probe)
        if failed and len(failed) < total:
            bugs.append((source, site.description))
        if len(bugs) >= limit:
            break
    return bugs


def _tool_python() -> str:
    """The gate tool venv's interpreter (it has pytest and hypothesis)."""
    from ..sandbox.local import LocalSandbox
    box = LocalSandbox(Path("."))
    box.ensure()
    return str(box.python)


# -- running -------------------------------------------------------------------------------

def judge(project: Path, config: GateConfig) -> GateReport:
    return GateRunner(project, config).run()


def classify_case(kind: str, report: GateReport) -> str:
    if kind == GOOD:
        return "correct" if report.passed else "FALSE_ALARM"
    if kind in (WRONG_GREEN, WRONG_VIBE):
        return "caught" if not report.passed else "FALSE_CONFIDENCE"
    return "flagged_thin_tests" if not report.passed else "accepted"


def run_calibration(
    tasks: list[Task],
    out: Path,
    *,
    mutants_per_task: int = 2,
    wrong_answers: dict[str, tuple[str, str]] | None = None,
    config: GateConfig | None = None,
    seed: int = 20261102,
    log: Callable[[str], None] = lambda m: print(m, flush=True),
    judge_fn: Callable[[Path, GateConfig], GateReport] = judge,
) -> list[CaseResult]:
    config = config or GateConfig(sandbox="local")
    out.mkdir(parents=True, exist_ok=True)
    results: list[CaseResult] = []

    def record(task: Task, kind: str, source_kind: str, bug: str, project: Path,
               kept: int = 0, dropped: int = 0) -> None:
        started = time.monotonic()
        report = judge_fn(project, config)
        statuses = {r.gate_id: r.status.value for r in report.results}
        caught = [r.gate_id for r in report.results if r.blocked]
        result = CaseResult(task.id, kind, source_kind, bug, report.passed,
                            report.verdict(), statuses, caught, kept, dropped,
                            round(time.monotonic() - started, 1))
        results.append(result)
        (out / "results.json").write_text(
            json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
        log(f"  {kind:12} {source_kind or '-':11} {classify_case(kind, report):18} "
            f"{report.verdict():19} {','.join(caught) or '-'}")

    for index, task in enumerate(tasks, start=1):
        log(f"[{index}/{len(tasks)}] {task.id}")
        base = out / task.id
        good = write_project(base / "good", task.module, task.reference, task.hidden_tests)
        record(task, GOOD, "", "", good)

        bugs = [("mutant", src, desc) for src, desc in
                mutant_bugs(task, base / "scratch", mutants_per_task, seed)]
        if wrong_answers and task.id in wrong_answers:
            bugs.append(("handwritten", wrong_answers[task.id][1], "hand-written wrong answer"))

        for n, (source_kind, source, description) in enumerate(bugs, start=1):
            probe = write_project(base / f"probe{n}", task.module, source, task.hidden_tests)
            failed, total = failing_tests(probe)
            if not failed or len(failed) >= total:
                continue  # no green-but-wrong project can be built from this bug
            wrong = write_project(base / f"wrong{n}", task.module, source,
                                  task.hidden_tests, deselected=failed)
            record(task, WRONG_GREEN, source_kind, description, wrong,
                   kept=total - len(failed), dropped=len(failed))
            thin = write_project(base / f"thin{n}", task.module, task.reference,
                                 task.hidden_tests, deselected=failed)
            record(task, RIGHT_THIN, source_kind, description, thin,
                   kept=total - len(failed), dropped=len(failed))

            # The realistic case: a couple of happy-path tests that pass.
            every = collected_tests(probe)
            keep = [i for i in every if i not in failed][:VIBE_TESTS]
            vibe = write_project(base / f"vibe{n}", task.module, source,
                                 task.hidden_tests,
                                 deselected=[i for i in every if i not in keep])
            record(task, WRONG_VIBE, source_kind, description, vibe,
                   kept=len(keep), dropped=total - len(keep))
        shutil.rmtree(base / "scratch", ignore_errors=True)
    return results


# -- reporting -------------------------------------------------------------------------------

def _rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": k / n if n else None, "ci": wilson(k, n)}


def _wrong_block(cases: list[CaseResult]) -> dict:
    by_source = {}
    for source in ("mutant", "handwritten"):
        sel = [r for r in cases if r.bug_source == source]
        by_source[source] = {
            "caught": _rate(sum(1 for r in sel if not r.passed), len(sel)),
            "caught_without_g4": _rate(
                sum(1 for r in sel if set(r.caught_by) - {"G4"}), len(sel)),
        }
    gates: dict[str, int] = {}
    for r in cases:
        for gate in r.caught_by:
            gates[gate] = gates.get(gate, 0) + 1
    return {
        "caught": _rate(sum(1 for r in cases if not r.passed), len(cases)),
        "by_source": by_source,
        "caught_by_gate": dict(sorted(gates.items())),
        "baseline_tests_pass_caught": _rate(0, len(cases)),
        "false_confidence": [f"{r.task_id}: {r.bug}" for r in cases if r.passed],
    }


def summarize(results: list[CaseResult]) -> dict:
    good = [r for r in results if r.kind == GOOD]
    thin = [r for r in results if r.kind == RIGHT_THIN]
    return {
        "cases": len(results),
        "good_passed": _rate(sum(1 for r in good if r.passed), len(good)),
        "false_alarm_gates": {r.task_id: [g for g, s in r.gate_statuses.items()
                                          if s in ("FAIL", "ERROR")]
                              for r in good if not r.passed},
        "vibe": _wrong_block([r for r in results if r.kind == WRONG_VIBE]),
        "adversarial": _wrong_block([r for r in results if r.kind == WRONG_GREEN]),
        "thin_tests_flagged": _rate(sum(1 for r in thin if not r.passed), len(thin)),
    }


def render_markdown(summary: dict) -> str:
    def pct(entry: dict) -> str:
        if entry["rate"] is None:
            return "n/a"
        low, high = entry["ci"]
        return f"{entry['k']}/{entry['n']} ({entry['rate']:.0%}, 95% CI {low:.0%}-{high:.0%})"

    lines = [
        "# Gate calibration",
        "",
        "Every case has a known right answer. \"Tests pass\" is what a typical "
        "AI tool (or a vibe coder) checks before calling code done.",
        "",
        "| Question | CodeCrew | \"Tests pass\" |",
        "|---|---|---|",
        f"| Correct code + its good tests: passed | {pct(summary['good_passed'])} | "
        f"{pct(summary['good_passed'])} |",
    ]
    for key, label in (("vibe", "Wrong code + a few passing happy-path tests: caught"),
                       ("adversarial", "Wrong code + every test except the one that "
                                       "exposes the bug: caught")):
        block = summary[key]
        lines.append(f"| {label} | {pct(block['caught'])} | "
                     f"{pct(block['baseline_tests_pass_caught'])} |")
        lines.append(f"| ... hand-written wrong answers | "
                     f"{pct(block['by_source']['handwritten']['caught'])} | 0 |")
        lines.append(f"| ... mutants (any gate / without G4) | "
                     f"{pct(block['by_source']['mutant']['caught'])} / "
                     f"{pct(block['by_source']['mutant']['caught_without_g4'])} | 0 |")
    lines += [
        f"| Correct code + those near-complete tests: flagged as under-tested | "
        f"{pct(summary['thin_tests_flagged'])} | 0 |",
        "",
    ]
    for key, label in (("vibe", "happy-path"), ("adversarial", "near-complete")):
        block = summary[key]
        gates = ", ".join(f"{g} {n}" for g, n in block["caught_by_gate"].items()) or "none"
        lines.append(f"Gates that failed on wrong code ({label} tests): {gates}")
        if block["false_confidence"]:
            lines.append(f"Missed ({label} tests): " + "; ".join(block["false_confidence"]))
        lines.append("")
    if summary["false_alarm_gates"]:
        lines.append("False alarms (correct code rejected): " + "; ".join(
            f"{t} ({', '.join(g)})" for t, g in summary["false_alarm_gates"].items()))
    return "\n".join(lines).rstrip() + "\n"


def load(out: Path) -> list[CaseResult]:
    data = json.loads((out / "results.json").read_text(encoding="utf-8"))
    return [CaseResult(**row) for row in data]


