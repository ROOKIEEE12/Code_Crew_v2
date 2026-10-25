"""The Phase 6 study: resumability, the two judges, and the statistics.

Everything here runs offline with an injected cell runner. The statistics
are checked against values computed by hand, because a wrong interval would
silently change what the evaluation is allowed to claim.
"""

from __future__ import annotations

import json

import pytest

from codecrew.__main__ import main as cli
from codecrew.bench.ablation import (
    Study,
    StudyMismatch,
    claims_ok,
    list_evaluations,
    mcnemar_exact,
    render_markdown,
    summarize_study,
    task_fingerprint,
    wilson,
)
from codecrew.bench.runner import TaskOutcome
from codecrew.bench.tasks import TASKS
from codecrew.llm import RateLimited

TWO = list(TASKS[:2])


def outcome(task, *, claimed, hidden, g2="PASS", built=True, tokens=100):
    return TaskOutcome(
        task_id=task.id, tier=task.tier, built=built,
        verdict="VERIFIED" if claimed else "UNVERIFIED",
        claimed_ok=claimed, hidden_passed=hidden if built else None,
        total_tokens=tokens, mutation_score=60.0,
        gate_statuses={"G1": "PASS", "G2": g2} if built else {},
    )


def fake_runner(table):
    """table[(condition, task_id)] -> kwargs for outcome()."""
    calls = []

    def run(condition, task, out_dir):
        calls.append((condition, task.id, out_dir.name))
        return outcome(task, **table[(condition, task.id)])
    run.calls = calls
    return run


def study(tmp_path, runner, reps=1, conditions=("single", "multi"), tasks=TWO):
    return Study(tmp_path, "s", tasks=list(tasks), conditions=list(conditions),
                 reps=reps, cell_runner=runner)


TABLE = {
    # single: claims ok on both, one is wrong -> 1 false confidence
    ("single", TWO[0].id): dict(claimed=True, hidden=True),
    ("single", TWO[1].id): dict(claimed=True, hidden=False),
    # multi: own tests pass on both, gates reject the wrong one
    ("multi", TWO[0].id): dict(claimed=True, hidden=True),
    ("multi", TWO[1].id): dict(claimed=False, hidden=False),
}


# -- statistics -------------------------------------------------------------------

def test_wilson_matches_hand_computed_values():
    low, high = wilson(0, 10)
    assert low == 0.0 and high == pytest.approx(0.2775, abs=1e-3)
    low, high = wilson(5, 10)
    assert (low, high) == (pytest.approx(0.2366, abs=1e-3), pytest.approx(0.7634, abs=1e-3))
    assert wilson(0, 0) is None


def test_mcnemar_exact():
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(0, 5) == pytest.approx(0.0625)
    assert mcnemar_exact(1, 6) == pytest.approx(0.125)
    assert mcnemar_exact(3, 3) == 1.0


def test_the_own_tests_judge_ignores_every_deep_gate():
    run = {"built": True, "claimed_ok": False,
           "gate_statuses": {"G1": "PASS", "G2": "PASS", "G4": "FAIL", "G7": "FAIL"}}
    assert claims_ok(run, "own_tests") is True
    assert claims_ok(run, "full") is False
    assert claims_ok({**run, "built": False}, "own_tests") is False
    assert claims_ok({**run, "gate_statuses": {"G1": "PASS", "G2": "FAIL"}},
                     "own_tests") is False


# -- running ---------------------------------------------------------------------------

def test_cells_run_repetition_major_and_are_saved(tmp_path):
    runner = fake_runner(TABLE)
    s = study(tmp_path, runner, reps=2)
    result = s.run(log=lambda _: None)
    assert result == {"ran": 8, "pending": 0, "stopped": None}
    # every condition of rep 1 before any of rep 2
    reps = [c[2] for c in runner.calls]
    assert reps == ["rep1"] * 4 + ["rep2"] * 4
    saved = json.loads((s.path / "multi" / "rep2" / f"{TWO[1].id}.json").read_text())
    assert saved["condition"] == "multi" and saved["rep"] == 2


def test_a_quota_error_stops_the_study_and_leaves_the_cell_pending(tmp_path):
    seen = []

    def runner(condition, task, out_dir):
        seen.append((condition, task.id))
        if len(seen) == 3:
            raise RateLimited("daily token quota exhausted")
        return outcome(task, **TABLE[(condition, task.id)])

    s = study(tmp_path, runner)
    first = s.run(log=lambda _: None)
    assert first["ran"] == 2 and first["pending"] == 2
    assert "quota" in first["stopped"]

    # Resuming runs only what is missing, starting with the failed cell.
    resumed = fake_runner(TABLE)
    s2 = study(tmp_path, resumed)
    assert s2.run(log=lambda _: None)["pending"] == 0
    assert [c[:2] for c in resumed.calls] == [seen[2], ("multi", TWO[1].id)]


def test_a_locked_leftover_workspace_does_not_stop_the_study(tmp_path, monkeypatch):
    """Found live: OneDrive held an interrupted run's folder open on Windows."""
    import shutil

    from codecrew.bench.ablation import _fresh_cell_dir

    leftover = tmp_path / "rep1" / "semver" / ".codecrew"
    leftover.mkdir(parents=True)
    assert _fresh_cell_dir(tmp_path / "rep1", "semver") == tmp_path / "rep1"
    assert not (tmp_path / "rep1" / "semver").exists(), "removable leftovers are removed"

    leftover.mkdir(parents=True)
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)  # cannot delete
    assert _fresh_cell_dir(tmp_path / "rep1", "semver") == tmp_path / "rep1" / "attempt2"


def test_max_cells_limits_one_invocation(tmp_path):
    s = study(tmp_path, fake_runner(TABLE))
    assert s.run(max_cells=1, log=lambda _: None) == {"ran": 1, "pending": 3, "stopped": None}


def test_a_study_refuses_to_mix_benchmark_revisions(tmp_path):
    study(tmp_path, fake_runner(TABLE)).prepare()
    with pytest.raises(StudyMismatch, match="tasks"):
        study(tmp_path, fake_runner(TABLE), tasks=TASKS[:3]).prepare()
    with pytest.raises(StudyMismatch, match="conditions"):
        study(tmp_path, fake_runner(TABLE), conditions=("single",)).prepare()
    # but more repetitions may be added
    study(tmp_path, fake_runner(TABLE), reps=3).prepare()


def test_fingerprint_changes_with_a_hidden_test():
    from dataclasses import replace
    changed = [replace(TWO[0], hidden_tests=TWO[0].hidden_tests + "\n# edit"), TWO[1]]
    assert task_fingerprint(TWO) != task_fingerprint(changed)


@pytest.mark.parametrize("kwargs", [dict(conditions=("swarm",)), dict(reps=0)])
def test_bad_study_settings_are_refused(tmp_path, kwargs):
    with pytest.raises(ValueError):
        study(tmp_path, fake_runner(TABLE), **kwargs)


# -- analysis --------------------------------------------------------------------------

def test_summary_separates_architecture_from_judge(tmp_path):
    s = study(tmp_path, fake_runner(TABLE))
    s.run(log=lambda _: None)
    summary = summarize_study(s.path)
    rows = {(r["condition"], r["judge"]): r for r in summary["conditions"]}

    assert rows[("single", "full")]["false_confidence"] == 1
    assert rows[("single", "full")]["false_confidence_rate"] == 0.5
    assert rows[("multi", "full")]["false_confidence"] == 0
    # Judged by its own tests alone, multi would have been wrong too.
    assert rows[("multi", "own_tests")]["false_confidence"] == 1

    gv = {g["condition"]: g for g in summary["gate_value"]}
    assert gv["multi"] == {"condition": "multi", "own_tests_false_confidence": 1,
                           "caught_by_full_gates": 1,
                           "extra_rejections_of_correct_code": 0}
    assert summary["per_task"][TWO[1].id] == {"single": "0/1 (1 FC)", "multi": "0/1"}
    assert summary["completed"] == 4 and summary["pending"] == 0

    paired = summary["paired"][0]
    assert (paired["a"], paired["b"], paired["pairs"]) == ("single", "multi", 2)
    assert paired["a_only_pass"] == 0 and paired["b_only_pass"] == 0


def test_report_and_listing(tmp_path):
    s = study(tmp_path, fake_runner(TABLE))
    s.run(log=lambda _: None)
    text = render_markdown(summarize_study(s.path))
    assert "| single | full | 2 | 2 | 1 (50% [" in text
    assert "Overlapping intervals mean" in text
    assert [e["name"] for e in list_evaluations(tmp_path)] == ["s"]
    assert list_evaluations(tmp_path / "missing") == []


def test_cli_report_writes_files(tmp_path, capsys):
    s = study(tmp_path, fake_runner(TABLE))
    s.run(log=lambda _: None)
    assert cli(["eval", "--root", str(tmp_path), "--name", "s", "--report"]) == 0
    assert (s.path / "report.md").exists() and (s.path / "summary.json").exists()
    assert "# Evaluation: s" in capsys.readouterr().out
    assert cli(["eval", "--root", str(tmp_path), "--name", "nope", "--report"]) == 2
