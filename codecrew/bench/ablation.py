"""Phase 6: the evaluation study and its ablations.

Two factors, each isolating one claim the project makes:

**Architecture** (what writes the code and tests), three conditions run as
separate generations:

=================  ==========================================================
``single``         one agent writes code and its own tests (the baseline)
``multi``          spec -> independent implementer and test author
``multi_repair``   ``multi`` plus up to two reconciliation rounds
=================  ==========================================================

**Judge** (what decides "done"), two judges applied to *the same runs*:

=================  ==========================================================
``full``           every blocking gate (CodeCrew's verdict)
``own_tests``      the code compiles and its own tests pass (G1 + G2) - what
                   a typical AI coding tool checks before saying "done"
=================  ==========================================================

``single`` and ``multi`` crossed with the two judges is the four-way
ablation; ``multi_repair`` is reported alongside it. Applying both judges to
the same runs costs no extra model calls and removes generation noise from
the judge comparison: any difference is the judge's alone.

The study is resumable. Free-tier quotas end runs mid-way, so every finished
cell is written to disk at once, a quota error stops the study cleanly, and
the next invocation continues where it stopped. Cells run repetition-major,
so a partial study is balanced across conditions.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterable

from ..config import GateConfig
from ..llm import LLMConfig, LLMError, ProviderUnavailable
from .runner import BenchRunner, TaskOutcome
from .tasks import Task

CONDITIONS: dict[str, tuple[str, int]] = {
    "single": ("single", 0),
    "multi": ("multi", 0),
    "multi_repair": ("multi", 2),
}
JUDGES = ("full", "own_tests")
STUDY_FILE = "study.json"

CellRunner = Callable[[str, Task, Path], TaskOutcome]


def _say(message: str) -> None:
    # Flushed, because a study usually runs with its output redirected.
    print(message, flush=True)


class StudyMismatch(ValueError):
    """The study on disk was started with different tasks or settings."""


def task_fingerprint(tasks: Iterable[Task]) -> str:
    """Changes whenever a requirement or hidden test changes.

    Results gathered against a different benchmark are not comparable, so a
    study refuses to mix them rather than averaging across revisions.
    """
    digest = hashlib.sha256()
    for task in tasks:
        digest.update(task.id.encode())
        digest.update(task.requirement.encode())
        digest.update(task.hidden_tests.encode())
    return digest.hexdigest()[:16]


class Study:
    def __init__(
        self,
        root: Path,
        name: str,
        *,
        tasks: list[Task],
        conditions: list[str],
        reps: int,
        llm_config: LLMConfig | None = None,
        gate_config: GateConfig | None = None,
        cell_runner: CellRunner | None = None,
    ) -> None:
        unknown = [c for c in conditions if c not in CONDITIONS]
        if unknown:
            raise ValueError(f"unknown condition(s): {', '.join(unknown)}; "
                             f"known: {', '.join(CONDITIONS)}")
        if reps < 1:
            raise ValueError("reps must be at least 1")
        if not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("study name may contain letters, digits, - and _")
        self.path = root.resolve() / name
        self.name = name
        self.tasks = tasks
        self.conditions = conditions
        self.reps = reps
        self.llm_config = llm_config or LLMConfig()
        self.gate_config = gate_config or GateConfig()
        self._cell_runner = cell_runner or self._run_cell

    # -- bookkeeping ------------------------------------------------------------

    def meta(self) -> dict:
        return {
            "name": self.name,
            "tasks": [t.id for t in self.tasks],
            "task_fingerprint": task_fingerprint(self.tasks),
            "conditions": self.conditions,
            "reps": self.reps,
            "provider": self.llm_config.provider,
            "temperature": self.llm_config.temperature,
            "disabled_gates": list(self.gate_config.disabled),
            "max_mutants": self.gate_config.thresholds.max_mutants,
        }

    def prepare(self) -> None:
        """Create the study, or check that the one on disk is the same study."""
        self.path.mkdir(parents=True, exist_ok=True)
        file = self.path / STUDY_FILE
        meta = self.meta()
        if file.exists():
            saved = json.loads(file.read_text(encoding="utf-8"))
            for key in ("tasks", "task_fingerprint", "conditions",
                        "disabled_gates", "max_mutants"):
                if saved.get(key) != meta[key]:
                    raise StudyMismatch(
                        f"study {self.name!r} was started with a different "
                        f"{key.replace('_', ' ')}; start a new study name "
                        f"instead of mixing results")
            # More repetitions may be added to an existing study.
            saved["reps"] = max(saved.get("reps", 0), self.reps)
            saved["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            file.write_text(json.dumps(saved, indent=2), encoding="utf-8")
            return
        meta["created"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        file.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def cells(self) -> list[tuple[int, str, Task]]:
        """Repetition-major order, so a partial study stays balanced."""
        return [(rep, condition, task)
                for rep in range(1, self.reps + 1)
                for task in self.tasks
                for condition in self.conditions]

    def cell_dir(self, rep: int, condition: str) -> Path:
        return self.path / condition / f"rep{rep}"

    def outcome_file(self, rep: int, condition: str, task: Task) -> Path:
        return self.cell_dir(rep, condition) / f"{task.id}.json"

    def pending(self) -> list[tuple[int, str, Task]]:
        return [c for c in self.cells() if not self.outcome_file(*c).exists()]

    # -- running ------------------------------------------------------------------

    def run(self, *, max_cells: int | None = None,
            log: Callable[[str], None] | None = None) -> dict:
        """Run pending cells. Returns {"ran", "pending", "stopped"}."""
        say = log or _say
        self.prepare()
        todo = self.pending()
        if max_cells is not None:
            todo = todo[:max_cells]
        ran = 0
        stopped = None
        for index, (rep, condition, task) in enumerate(todo, start=1):
            say(f"[{index}/{len(todo)}] rep {rep}  {condition:13} {task.id} ...")
            started = time.monotonic()
            try:
                outcome = self._cell_runner(condition, task, self.cell_dir(rep, condition))
            except (LLMError, ProviderUnavailable) as exc:
                # Quota or provider trouble is not a result about the code.
                # Leave the cell pending; the next invocation resumes here.
                stopped = f"{type(exc).__name__}: {exc}"
                say(f"      stopped, cell left pending: {stopped[:200]}")
                break
            record = {**asdict(outcome), "classification": outcome.classification,
                      "condition": condition, "rep": rep,
                      "wall_s": round(time.monotonic() - started, 1)}
            file = self.outcome_file(rep, condition, task)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps(record, indent=2), encoding="utf-8")
            ran += 1
            say(f"      {outcome.classification:17} verdict={outcome.verdict}")
        return {"ran": ran, "pending": len(self.pending()), "stopped": stopped}

    def _run_cell(self, condition: str, task: Task, out_dir: Path) -> TaskOutcome:
        mode, repair = CONDITIONS[condition]
        runner = BenchRunner(self.llm_config, self.gate_config, out_dir,
                             mode=mode, repair_rounds=repair)
        out_dir.mkdir(parents=True, exist_ok=True)
        return runner.run_task(task)


# -- analysis --------------------------------------------------------------------

def own_tests_claim(outcome: dict) -> bool:
    """What a typical AI tool calls done: it compiles and its tests pass."""
    statuses = outcome.get("gate_statuses") or {}
    return statuses.get("G1") == "PASS" and statuses.get("G2") == "PASS"


def claims_ok(outcome: dict, judge: str) -> bool:
    if not outcome.get("built"):
        return False
    if judge == "full":
        return bool(outcome.get("claimed_ok"))
    if judge == "own_tests":
        return own_tests_claim(outcome)
    raise ValueError(f"unknown judge {judge!r}")


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float] | None:
    """95% Wilson score interval: sensible at small n and at 0 or n successes."""
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the two discordant counts."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def load_outcomes(path: Path) -> list[dict]:
    out = []
    for file in sorted(path.glob("*/rep*/*.json")):
        try:
            out.append(json.loads(file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize_study(path: Path) -> dict:
    meta = json.loads((path / STUDY_FILE).read_text(encoding="utf-8"))
    outcomes = load_outcomes(path)
    conditions = [c for c in meta.get("conditions", []) if c in CONDITIONS]
    total_cells = len(meta.get("tasks", [])) * len(conditions) * int(meta.get("reps", 0))

    rows = []
    for condition in conditions:
        sel = [o for o in outcomes if o.get("condition") == condition]
        hidden = sum(1 for o in sel if o.get("hidden_passed") is True)
        for judge in JUDGES:
            claimed = [o for o in sel if claims_ok(o, judge)]
            fc = sum(1 for o in claimed if o.get("hidden_passed") is False)
            rows.append({
                "condition": condition,
                "judge": judge,
                "runs": len(sel),
                "built": sum(1 for o in sel if o.get("built")),
                "claimed_ok": len(claimed),
                "false_confidence": fc,
                "false_confidence_rate": fc / len(claimed) if claimed else None,
                "fc_ci": wilson(fc, len(claimed)),
                # Judge-independent, repeated per judge for a flat table.
                "hidden_pass_rate": hidden / len(sel) if sel else None,
                "hp_ci": wilson(hidden, len(sel)),
                "mean_mutation_score": _mean([o["mutation_score"] for o in sel
                                              if o.get("mutation_score") is not None]),
                "mean_tokens": _mean([o.get("total_tokens", 0) for o in sel]) if sel else None,
            })

    def key(o: dict) -> tuple:
        return (o.get("task_id"), o.get("rep"))

    paired = []
    for i, a in enumerate(conditions):
        for b in conditions[i + 1:]:
            left = {key(o): o for o in outcomes if o.get("condition") == a}
            right = {key(o): o for o in outcomes if o.get("condition") == b}
            shared = sorted(set(left) & set(right), key=str)
            a_only = sum(1 for k in shared if left[k].get("hidden_passed") is True
                         and right[k].get("hidden_passed") is not True)
            b_only = sum(1 for k in shared if right[k].get("hidden_passed") is True
                         and left[k].get("hidden_passed") is not True)
            paired.append({"a": a, "b": b, "pairs": len(shared),
                           "a_only_pass": a_only, "b_only_pass": b_only,
                           "p_value": mcnemar_exact(a_only, b_only)})

    gate_value = []
    for condition in conditions:
        sel = [o for o in outcomes if o.get("condition") == condition and o.get("built")]
        weak_fc = [o for o in sel if claims_ok(o, "own_tests")
                   and o.get("hidden_passed") is False]
        gate_value.append({
            "condition": condition,
            "own_tests_false_confidence": len(weak_fc),
            "caught_by_full_gates": sum(1 for o in weak_fc if not claims_ok(o, "full")),
            "extra_rejections_of_correct_code": sum(
                1 for o in sel if claims_ok(o, "own_tests")
                and not claims_ok(o, "full") and o.get("hidden_passed") is True),
        })

    per_task: dict[str, dict[str, str]] = {}
    for task_id in meta.get("tasks", []):
        per_task[task_id] = {}
        for condition in conditions:
            sel = [o for o in outcomes
                   if o.get("task_id") == task_id and o.get("condition") == condition]
            if sel:
                passed = sum(1 for o in sel if o.get("hidden_passed") is True)
                fc = sum(1 for o in sel if o.get("classification") == "FALSE_CONFIDENCE")
                per_task[task_id][condition] = (
                    f"{passed}/{len(sel)}" + (f" ({fc} FC)" if fc else ""))

    return {
        "name": meta.get("name", path.name),
        "path": str(path),
        "meta": meta,
        "conditions": rows,
        "paired": paired,
        "gate_value": gate_value,
        "per_task": per_task,
        "completed": len(outcomes),
        "pending": max(0, total_cells - len(outcomes)),
    }


def list_evaluations(root: Path) -> list[dict]:
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir()):
        if (child / STUDY_FILE).is_file():
            try:
                out.append(summarize_study(child))
            except (OSError, ValueError, KeyError):
                continue
    return out


def render_markdown(summary: dict) -> str:
    def pct(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.0%}"

    def ci(v) -> str:
        return "" if not v else f" [{v[0]:.0%}, {v[1]:.0%}]"

    meta = summary["meta"]
    lines = [
        f"# Evaluation: {summary['name']}",
        "",
        f"Provider `{meta.get('provider')}`, temperature {meta.get('temperature')}, "
        f"{len(meta.get('tasks', []))} tasks x {len(meta.get('conditions', []))} "
        f"conditions x {meta.get('reps')} repetitions. "
        f"{summary['completed']} cells complete, {summary['pending']} pending. "
        f"Task fingerprint `{meta.get('task_fingerprint')}`.",
        "",
        "Intervals are 95% Wilson score intervals. Overlapping intervals mean "
        "this data cannot rank the conditions.",
        "",
        "| Architecture | Judge | Runs | Claimed OK | False confidence | "
        "Hidden tests pass | Mean mutation | Tokens/run |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in summary["conditions"]:
        mut = "n/a" if r["mean_mutation_score"] is None else f"{r['mean_mutation_score']:.0f}%"
        tok = "n/a" if r["mean_tokens"] is None else f"{r['mean_tokens']:.0f}"
        lines.append(
            f"| {r['condition']} | {r['judge']} | {r['runs']} | {r['claimed_ok']} | "
            f"{r['false_confidence']} ({pct(r['false_confidence_rate'])}{ci(r['fc_ci'])}) | "
            f"{pct(r['hidden_pass_rate'])}{ci(r['hp_ci'])} | {mut} | {tok} |")

    lines += ["", "**What the deep gates add over \"its own tests pass\"** "
              "(same runs, two judges)", "",
              "| Architecture | Own-tests false confidence | Caught by full gates | "
              "Correct code rejected by full gates |", "|---|---|---|---|"]
    for g in summary["gate_value"]:
        lines.append(f"| {g['condition']} | {g['own_tests_false_confidence']} | "
                     f"{g['caught_by_full_gates']} | {g['extra_rejections_of_correct_code']} |")

    if summary["paired"]:
        lines += ["", "**Paired comparison of hidden-test passes** (same task and "
                  "repetition; exact McNemar test)", "",
                  "| A | B | Pairs | Only A passed | Only B passed | p |",
                  "|---|---|---|---|---|---|"]
        for p in summary["paired"]:
            lines.append(f"| {p['a']} | {p['b']} | {p['pairs']} | {p['a_only_pass']} | "
                         f"{p['b_only_pass']} | {p['p_value']:.2f} |")

    lines += ["", "**Per task** (hidden passes / runs; FC = false confidence under "
              "the full judge)", "",
              "| Task | " + " | ".join(meta.get("conditions", [])) + " |",
              "|---|" + "---|" * len(meta.get("conditions", []))]
    for task_id, cells in summary["per_task"].items():
        lines.append(f"| {task_id} | " + " | ".join(
            cells.get(c, "-") for c in meta.get("conditions", [])) + " |")
    return "\n".join(lines) + "\n"
