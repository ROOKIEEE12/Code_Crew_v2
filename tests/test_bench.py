"""Tests for the benchmark itself.

A benchmark nobody has validated is just a guess with a name. These tests
establish that every task is satisfiable (its reference passes the hidden
tests), that the hidden tests actually reject wrong code, and that the
confusion-matrix classification does what the report says it does.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from codecrew.bench import (
    BUILD_FAILED,
    CAUGHT,
    FALSE_ALARM,
    FALSE_CONFIDENCE,
    TASKS,
    TRUE_PASS,
    BenchRunner,
    TaskOutcome,
    get_tasks,
    summarize,
)
from codecrew.config import GateConfig
from codecrew.llm import LLMConfig, ScriptedProvider


def run_hidden(tmp_path: Path, module: str, source: str, hidden: str):
    (tmp_path / f"{module}.py").write_text(source.lstrip("\n"), encoding="utf-8")
    (tmp_path / "test_hidden.py").write_text(hidden.lstrip("\n"), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "test_hidden.py", "-q",
         "-p", "no:cacheprovider", "--no-header"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )


# -- validity of the benchmark ------------------------------------------------

@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_reference_solution_passes_the_hidden_tests(task, tmp_path: Path):
    """Every task must be satisfiable, or a failure means nothing."""
    result = run_hidden(tmp_path, task.module, task.reference, task.hidden_tests)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_a_module_that_does_nothing_fails_the_hidden_tests(task, tmp_path: Path):
    """The hidden tests must be able to say no."""
    result = run_hidden(tmp_path, task.module, "# intentionally empty\n",
                        task.hidden_tests)
    assert result.returncode != 0


def test_task_ids_and_modules_are_unique():
    assert len({t.id for t in TASKS}) == len(TASKS)
    assert len({t.module for t in TASKS}) == len(TASKS)


def test_suite_spans_three_tiers_with_a_security_subset():
    tiers = {t.tier for t in TASKS}
    assert tiers == {"easy", "medium", "security"}
    assert sum(1 for t in TASKS if t.tier == "security") >= 2


def test_requirements_pin_the_module_name_the_hidden_tests_import():
    for task in TASKS:
        assert f"`{task.module}`" in task.requirement
        assert f"{task.module}.py" in task.requirement
        assert f"from {task.module} import" in task.hidden_tests


def test_unknown_task_id_is_reported_with_the_known_ones():
    with pytest.raises(KeyError, match="known:"):
        get_tasks(["nope"])


def test_task_subset_preserves_requested_order():
    assert [t.id for t in get_tasks(["roman", "clamp"])] == ["roman", "clamp"]


# -- the confusion matrix -----------------------------------------------------

def outcome(claimed: bool, hidden: bool | None, built: bool = True) -> TaskOutcome:
    return TaskOutcome(task_id="t", tier="easy", built=built, verdict="V",
                       claimed_ok=claimed, hidden_passed=hidden)


@pytest.mark.parametrize("claimed,hidden,built,expected", [
    (True, True, True, TRUE_PASS),
    (True, False, True, FALSE_CONFIDENCE),
    (False, False, True, CAUGHT),
    (False, True, True, FALSE_ALARM),
    (False, None, False, BUILD_FAILED),
])
def test_classification(claimed, hidden, built, expected):
    assert outcome(claimed, hidden, built).classification == expected


def test_false_confidence_rate_is_over_claims_not_over_tasks():
    """Denominator is 'said it was fine', not 'all tasks'."""
    outcomes = [
        outcome(True, True), outcome(True, False),      # 1 of 2 claims wrong
        outcome(False, False), outcome(False, None, built=False),
    ]
    summary = summarize(outcomes)
    assert summary.claimed_ok == 2
    assert summary.false_confidence == 1
    assert summary.false_confidence_rate == 0.5
    assert summary.build_failed == 1


def test_rate_is_none_when_nothing_was_claimed_ok():
    """Zero claims is not 0% false confidence; it is undefined."""
    summary = summarize([outcome(False, False), outcome(False, None, built=False)])
    assert summary.false_confidence_rate is None


# -- the harness, end to end (offline) ----------------------------------------

def submission(module: str, source: str, tests: str) -> str:
    return json.dumps({
        "files": [
            {"path": f"{module}.py", "content": source.lstrip("\n")},
            {"path": f"tests/test_{module}.py", "content": tests.lstrip("\n")},
        ],
        "notes": "scripted",
    })


@pytest.fixture
def scripted(monkeypatch):
    def factory(responses: list[str]) -> None:
        provider = ScriptedProvider(responses)
        monkeypatch.setattr(
            "codecrew.llm.client.select_provider",
            lambda name="auto", script=None: provider,
        )
    return factory


FAST_GATES = GateConfig(sandbox="local", disabled=("G3", "G4", "G6"))

CLAMP_TESTS = '''
from clamp import clamp

def test_inside():
    assert clamp(5, 0, 10) == 5

def test_below():
    assert clamp(-1, 0, 10) == 0
'''

BUGGY_CLAMP = '''
def clamp(value, low, high):
    return max(low, min(value, high))
'''


@pytest.mark.slow
def test_correct_solution_is_a_true_pass(scripted, tmp_path: Path):
    task = get_tasks(["clamp"])[0]
    scripted([submission("clamp", task.reference, CLAMP_TESTS)])
    runner = BenchRunner(LLMConfig(provider="scripted"), FAST_GATES, tmp_path)

    result = runner.run_task(task)

    assert result.built
    assert result.hidden_passed is True
    assert result.classification == TRUE_PASS


@pytest.mark.slow
def test_subtly_wrong_solution_is_false_confidence(scripted, tmp_path: Path):
    """The metric working: it passes its own tests, the gates are green, and
    the hidden tests expose the missing ValueError."""
    task = get_tasks(["clamp"])[0]
    scripted([submission("clamp", BUGGY_CLAMP, CLAMP_TESTS)])
    runner = BenchRunner(LLMConfig(provider="scripted"), FAST_GATES, tmp_path)

    result = runner.run_task(task)

    assert result.claimed_ok is True, "the cheap gates cannot see this bug"
    assert result.hidden_passed is False
    assert result.classification == FALSE_CONFIDENCE


@pytest.mark.slow
def test_hidden_tests_are_written_outside_the_agents_view(scripted, tmp_path: Path):
    task = get_tasks(["clamp"])[0]
    scripted([submission("clamp", task.reference, CLAMP_TESTS)])
    runner = BenchRunner(LLMConfig(provider="scripted"), FAST_GATES, tmp_path)
    runner.run_task(task)

    workspace = tmp_path / "clamp"
    hidden = workspace / ".codecrew" / "hidden" / "test_hidden.py"
    assert hidden.exists()
    # Not under tests/, so it can never be mistaken for the agent's own.
    assert not (workspace / "tests" / "test_hidden.py").exists()


def test_a_build_failure_is_scored_not_raised(scripted, tmp_path: Path):
    scripted(["not json", "still not json", "nope"])
    runner = BenchRunner(
        LLMConfig(provider="scripted", max_retries=1, backoff_base_s=0.0),
        FAST_GATES, tmp_path,
    )
    result = runner.run_task(get_tasks(["clamp"])[0])
    assert result.built is False
    assert result.classification == BUILD_FAILED
    assert result.hidden_passed is None
