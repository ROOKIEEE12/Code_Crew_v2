"""Phase 5 product surface: trust badges, the review caveat, the usage
guide, CI export and live progress.

The rule these tests hold the product to: nothing on the surface may claim
more than the gate report does. A badge is never better than the workspace
verdict, an unreviewed spec is always disclosed, and an example in the
usage guide is only called "executed" when G7 actually passed.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from codecrew.__main__ import main as cli
from codecrew.agents import Spec
from codecrew.badges import (
    PARTIAL,
    UNVERIFIED,
    VERIFIED,
    Review,
    compute_badges,
    source_files,
)
from codecrew.ci_export import export_workflow, render_workflow
from codecrew.config import GateConfig
from codecrew.docs import render_usage
from codecrew.gates import GateRunner
from codecrew.report import render_markdown
from codecrew.results import Finding, GateReport, GateResult, Severity, Status
from codecrew.trace import Tracer

def gate(gate_id: str, status: Status, findings=(), blocking=True, note=None):
    return GateResult(gate_id=gate_id, name=gate_id, status=status, summary="s",
                      findings=list(findings), blocking=blocking, note=note)


def finding(file, severity=Severity.HIGH, line=None):
    return Finding(severity=severity, code="c", message="m", file=file, line=line)


@pytest.fixture
def two_modules(tmp_path: Path) -> Path:
    (tmp_path / "stats.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "words.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_stats.py").write_text("", encoding="utf-8")
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    return tmp_path


def badges_of(report: GateReport, workspace: Path) -> dict[str, str]:
    return {b.path: b.badge for b in compute_badges(report, workspace)}


# -- badges ---------------------------------------------------------------------

def test_only_code_under_test_gets_a_badge(two_modules: Path):
    assert source_files(two_modules) == ["stats.py", "words.py"]


def test_all_gates_passing_gives_verified(two_modules: Path):
    report = GateReport("r", str(two_modules), "s",
                        [gate("G1", Status.PASS), gate("G2", Status.PASS)])
    assert badges_of(report, two_modules) == {"stats.py": VERIFIED,
                                              "words.py": VERIFIED}


def test_a_failing_test_is_attributed_to_the_module_it_tests(two_modules: Path):
    report = GateReport("r", str(two_modules), "s", [gate(
        "G2", Status.FAIL, [finding("tests/test_stats.py")])])
    badges = badges_of(report, two_modules)
    assert badges["stats.py"] == UNVERIFIED
    # Not implicated, but the run failed: never better than partial.
    assert badges["words.py"] == PARTIAL


def test_an_absolute_path_from_a_tool_is_attributed(two_modules: Path):
    absolute = str((two_modules / "words.py").resolve())
    report = GateReport("r", str(two_modules), "s",
                        [gate("G1", Status.FAIL, [finding(absolute, line=3)])])
    badges = {b.path: b for b in compute_badges(report, two_modules)}
    assert badges["words.py"].badge == UNVERIFIED
    assert any("line 3" in reason for reason in badges["words.py"].reasons)


def test_a_failure_nobody_can_place_leaves_every_file_unverified(two_modules: Path):
    report = GateReport("r", str(two_modules), "s",
                        [gate("G2", Status.ERROR, note="pytest crashed")])
    assert set(badges_of(report, two_modules).values()) == {UNVERIFIED}


def test_a_skipped_blocking_gate_caps_every_file_at_partial(two_modules: Path):
    report = GateReport("r", str(two_modules), "s", [
        gate("G1", Status.PASS), gate("G4", Status.SKIP, note="blocked by G2")])
    badges = {b.path: b for b in compute_badges(report, two_modules)}
    assert {b.badge for b in badges.values()} == {PARTIAL}
    assert "blocked by G2" in badges["stats.py"].reasons[0]


def test_a_skipped_advisory_gate_is_a_note_not_a_downgrade(two_modules: Path):
    report = GateReport("r", str(two_modules), "s", [
        gate("G1", Status.PASS),
        gate("G6", Status.SKIP, blocking=False, note="no Hypothesis tests")])
    assert report.verdict() == VERIFIED
    badges = {b.path: b for b in compute_badges(report, two_modules)}
    assert {b.badge for b in badges.values()} == {VERIFIED}
    assert "no Hypothesis tests" in badges["stats.py"].notes[0]


def test_medium_findings_in_a_passing_gate_are_notes_not_downgrades(two_modules: Path):
    report = GateReport("r", str(two_modules), "s", [gate(
        "G4", Status.PASS, [finding("stats.py", Severity.MEDIUM)])])
    badges = {b.path: b for b in compute_badges(report, two_modules)}
    assert badges["stats.py"].badge == VERIFIED
    assert badges["stats.py"].notes


def test_a_high_finding_in_a_passing_gate_still_downgrades(two_modules: Path):
    report = GateReport("r", str(two_modules), "s", [gate(
        "G5", Status.PASS, [finding("stats.py", Severity.HIGH)])])
    assert badges_of(report, two_modules)["stats.py"] == PARTIAL


def test_an_advisory_failure_downgrades_only_its_file(two_modules: Path):
    report = GateReport("r", str(two_modules), "s", [gate(
        "G6", Status.FAIL, [finding("words.py", Severity.LOW)], blocking=False)])
    badges = badges_of(report, two_modules)
    assert badges == {"stats.py": VERIFIED, "words.py": PARTIAL}


@pytest.mark.parametrize("statuses", list(itertools.product(
    [Status.PASS, Status.FAIL, Status.SKIP, Status.ERROR], repeat=2)))
@pytest.mark.parametrize("blocking", [True, False])
def test_no_badge_is_better_than_the_workspace_verdict(two_modules, statuses, blocking):
    report = GateReport("r", str(two_modules), "s", [
        gate("G1", statuses[0], [finding("stats.py")], blocking=blocking),
        gate("G2", statuses[1], [finding("tests/test_stats.py")]),
    ])
    for badge in compute_badges(report, two_modules):
        if report.verdict() != VERIFIED:
            assert badge.badge != VERIFIED


# -- the review caveat ---------------------------------------------------------------

def test_an_unreviewed_spec_with_assumptions_is_disclosed():
    assert Review(reviewed=False, assumptions=2).caveat() == (
        "against an unreviewed spec with 2 open assumptions")
    assert Review(reviewed=False, assumptions=1).caveat().endswith("1 open assumption")


@pytest.mark.parametrize("review", [Review(True, 3), Review(False, 0)])
def test_no_caveat_when_reviewed_or_nothing_was_guessed(review):
    assert review.caveat() is None


def test_review_round_trips_through_the_workspace(tmp_path: Path):
    Review(reviewed=False, assumptions=4).write(tmp_path)
    assert Review.load(tmp_path) == Review(reviewed=False, assumptions=4)
    assert Review.load(tmp_path / "missing") is None


# -- the usage guide ---------------------------------------------------------------------

SPEC = Spec.from_dict({
    "summary": "Clamp a number.", "module": "clamp",
    "functions": [{"signature": "clamp(value: float, low: float, high: float) -> float",
                   "description": "Limit value.", "raises": ["ValueError"],
                   "examples": [{"call": "clamp(5, 0, 10)", "result": "5"},
                                {"call": "clamp(1, 5, 0)", "raises": "ValueError"}]}],
    "assumptions": ["bounds are inclusive"],
})


def test_usage_shows_examples_as_executed_only_when_g7_passed(tmp_path: Path):
    passed = GateReport("r", "w", "s", [gate("G7", Status.PASS)])
    text = render_usage([SPEC], passed)
    assert "was executed against the code" in text
    assert ">>> clamp(5, 0, 10)\n5" in text
    assert "from clamp import clamp" in text
    assert "bounds are inclusive" in text

    failed = GateReport("r", "w", "s", [gate("G7", Status.FAIL)])
    assert "Do not rely on them" in render_usage([SPEC], failed)
    assert "were **not** executed" in render_usage([SPEC], GateReport("r", "w", "s"))


def test_usage_carries_the_verdict_and_the_review_caveat():
    report = GateReport("r", "w", "s", [gate("G7", Status.PASS)])
    text = render_usage([SPEC], report, review=Review(False, 1))
    assert "**Verdict: VERIFIED** (against an unreviewed spec" in text


# -- markdown and CI ------------------------------------------------------------------------

def test_markdown_report_lists_every_gate_and_what_was_not_verified():
    report = GateReport("r", "w", "s", [
        gate("G1", Status.PASS),
        gate("G4", Status.SKIP, note="blocked by G2"),
        gate("G6", Status.SKIP, blocking=False, note="no property tests")])
    text = render_markdown(report, note="against an unreviewed spec")
    assert text.startswith("## CodeCrew: PARTIALLY VERIFIED")
    assert "| G4 G4 | ⚪ SKIP |" in text
    assert "| G6 G6 (advisory) | ⚪ SKIP |" in text
    assert "advisory: does not change the verdict" in text
    assert "blocked by G2" in text
    assert "against an unreviewed spec" in text


def test_the_workflow_runs_only_the_gates():
    text = render_workflow(workspace="app", ref="abc123", disable=("G4",))
    assert "codecrew verify \"app\" --sandbox local --disable G4" in text
    assert "@abc123" in text
    assert "$GITHUB_STEP_SUMMARY" in text
    assert "permissions:\n  contents: read" in text
    # No model, no secrets: CI must not need an API key to judge code.
    assert "API_KEY" not in text and "secrets." not in text


@pytest.mark.parametrize("bad", [
    {"workspace": "../outside"}, {"workspace": "/etc"},
    {"ref": "main; curl evil"}, {"source": "x$(id)"},
    {"python": "3.12; rm"}, {"disable": ("rm -rf",)},
])
def test_the_workflow_refuses_values_that_could_inject_shell(bad):
    with pytest.raises(ValueError):
        render_workflow(**bad)


def test_export_does_not_overwrite_without_force(tmp_path: Path):
    path = export_workflow(tmp_path)
    assert path == tmp_path / ".github" / "workflows" / "codecrew.yml"
    with pytest.raises(FileExistsError):
        export_workflow(tmp_path)
    export_workflow(tmp_path, force=True)


def test_cli_ci_export_prints(capsys):
    assert cli(["ci-export", "--print", "--ref", "v1"]) == 0
    assert "name: CodeCrew verification" in capsys.readouterr().out


# -- live progress ----------------------------------------------------------------------------

class _NoToolSandbox:
    name = "stub"
    strict = False

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def ensure(self) -> None:
        pass

    def has_module(self, module: str) -> bool:
        return False

    def run(self, args, *, timeout, cwd=None):
        raise AssertionError("must not execute")


def test_the_runner_reports_each_gate_as_it_finishes(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    events: list[dict] = []
    report = GateRunner(tmp_path, GateConfig(), sandbox=_NoToolSandbox(tmp_path),
                        progress=events.append).run()
    ends = [e for e in events if e["phase"] == "end"]
    assert [e["gate"] for e in ends] == [r.gate_id for r in report.results]
    assert all(e["status"] == r.status.value for e, r in zip(ends, report.results))


def test_a_broken_progress_observer_cannot_change_the_verdict(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    def explode(event):
        raise RuntimeError("ui crashed")

    quiet = GateRunner(tmp_path, GateConfig(), sandbox=_NoToolSandbox(tmp_path)).run()
    noisy = GateRunner(tmp_path, GateConfig(), sandbox=_NoToolSandbox(tmp_path),
                       progress=explode).run()
    assert [r.status for r in quiet.results] == [r.status for r in noisy.results]


def test_tracer_listeners_see_events_and_cannot_break_the_run(tmp_path: Path):
    tracer = Tracer(tmp_path / "t.jsonl")
    seen: list[dict] = []
    tracer.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    tracer.subscribe(seen.append)
    tracer.event("gate", gate="G1")
    assert seen[0]["kind"] == "gate" and seen[0]["gate"] == "G1"
    assert tracer.read()[0]["gate"] == "G1"
