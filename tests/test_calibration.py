"""The gate calibration harness: are the cases it builds what they claim?

The judge is replaced by a stub here; what is tested is that a "wrong but
green" project really is wrong and really is green, and that the summary
counts what it says it counts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codecrew.bench.calibration import (
    GOOD,
    RIGHT_THIN,
    WRONG_GREEN,
    WRONG_VIBE,
    CaseResult,
    classify_case,
    collected_tests,
    failing_tests,
    render_markdown,
    run_calibration,
    summarize,
    write_project,
)
from codecrew.bench.hard_tasks import KNOWN_WRONG
from codecrew.bench.tasks import get_tasks
from codecrew.results import GateReport, GateResult, Status

CSV = get_tasks(["csv_line"])[0]


def test_a_wrong_but_green_project_is_wrong_and_green(tmp_path: Path):
    wrong = KNOWN_WRONG["csv_line"][1]
    probe = write_project(tmp_path / "probe", CSV.module, wrong, CSV.hidden_tests)
    failed, total = failing_tests(probe)
    assert 0 < len(failed) < total, "some hidden tests expose the bug, some do not"

    green = write_project(tmp_path / "green", CSV.module, wrong, CSV.hidden_tests,
                          deselected=failed)
    assert failing_tests(green) == ([], total - len(failed)), "its own tests all pass"
    assert len(collected_tests(probe)) == total


def _report(passed: bool, failing_gate: str = "G4") -> GateReport:
    status = Status.PASS if passed else Status.FAIL
    return GateReport("r", "w", "s", [GateResult(failing_gate, failing_gate, status, "s")])


def test_cases_are_built_and_classified(tmp_path: Path):
    seen: list[str] = []

    def judge(project: Path, config):
        seen.append(project.name)
        return _report(passed=project.name == "good")  # reject every non-good case

    results = run_calibration([CSV], tmp_path, mutants_per_task=0,
                              wrong_answers=KNOWN_WRONG, judge_fn=judge,
                              log=lambda _: None)
    assert [r.kind for r in results] == [GOOD, WRONG_GREEN, RIGHT_THIN, WRONG_VIBE]
    vibe = results[-1]
    assert vibe.kept_tests == 2 and vibe.bug_source == "handwritten"
    assert (tmp_path / "results.json").exists()

    summary = summarize(results)
    assert summary["good_passed"]["k"] == 1
    assert summary["vibe"]["caught"]["k"] == 1
    assert summary["adversarial"]["caught"]["k"] == 1
    assert summary["vibe"]["baseline_tests_pass_caught"]["k"] == 0
    assert "| \"Tests pass\" |" in render_markdown(summary)


@pytest.mark.parametrize("kind,passed,label", [
    (GOOD, True, "correct"), (GOOD, False, "FALSE_ALARM"),
    (WRONG_GREEN, False, "caught"), (WRONG_VIBE, True, "FALSE_CONFIDENCE"),
    (RIGHT_THIN, False, "flagged_thin_tests"),
])
def test_classification(kind, passed, label):
    assert classify_case(kind, _report(passed)) == label


def test_catches_by_g4_alone_are_reported_apart():
    cases = [CaseResult("t", WRONG_GREEN, "mutant", "b", False, "UNVERIFIED",
                        caught_by=["G4"]),
             CaseResult("t", WRONG_GREEN, "mutant", "b", False, "UNVERIFIED",
                        caught_by=["G3", "G4"])]
    block = summarize(cases)["adversarial"]["by_source"]["mutant"]
    assert block["caught"]["k"] == 2
    assert block["caught_without_g4"]["k"] == 1
