"""End-to-end pipeline tests, run offline with the scripted provider.

These exercise the real gate layer against real generated files, with no
API key and no network, which is what makes the pipeline demonstrable under
a zero-budget constraint.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codecrew.config import GateConfig
from codecrew.llm import LLMConfig, ScriptedProvider
from codecrew.pipeline import Pipeline
from codecrew.results import Status

GOOD_SUBMISSION = json.dumps({
    "files": [
        {
            "path": "temperature.py",
            "content": (
                '"""Temperature conversion."""\n\n\n'
                "def celsius_to_fahrenheit(celsius: float) -> float:\n"
                '    """Convert Celsius to Fahrenheit."""\n'
                "    if celsius < -273.15:\n"
                '        raise ValueError("below absolute zero")\n'
                "    return celsius * 9 / 5 + 32\n"
            ),
        },
        {
            "path": "tests/test_temperature.py",
            "content": (
                "import pytest\n\n"
                "from temperature import celsius_to_fahrenheit\n\n\n"
                "def test_freezing():\n"
                "    assert celsius_to_fahrenheit(0) == 32\n\n\n"
                "def test_boiling():\n"
                "    assert celsius_to_fahrenheit(100) == 212\n\n\n"
                "def test_absolute_zero_is_allowed():\n"
                "    assert celsius_to_fahrenheit(-273.15) == pytest.approx(-459.67)\n\n\n"
                "def test_below_absolute_zero_raises():\n"
                "    with pytest.raises(ValueError):\n"
                "        celsius_to_fahrenheit(-273.16)\n"
            ),
        },
    ],
    "notes": "Linear conversion with an absolute-zero guard.",
    "self_assessment": "Floating point equality at the boundary.",
})

INSECURE_SUBMISSION = json.dumps({
    "files": [
        {
            "path": "runner.py",
            "content": (
                '"""Rule runner."""\n\n'
                'API_KEY = "3f8a1d6b9c20e4574af1b85d0c93e216a7d45f82"\n\n\n'
                "def run_rule(expression: str) -> object:\n"
                '    """Evaluate a rule."""\n'
                "    return eval(expression)\n"
            ),
        },
        {
            "path": "tests/test_runner.py",
            "content": (
                "from runner import run_rule\n\n\n"
                "def test_run_rule():\n"
                "    assert run_rule('1 + 1') == 2\n"
            ),
        },
    ],
    "notes": "Uses eval for flexibility.",
})


@pytest.fixture
def scripted(monkeypatch):
    """Force every LLMClient in the pipeline onto a scripted provider."""
    created: list[ScriptedProvider] = []

    def factory(responses: list[str]):
        provider = ScriptedProvider(responses)
        created.append(provider)
        monkeypatch.setattr(
            "codecrew.llm.client.select_provider",
            lambda name="auto", script=None: provider,
        )
        return provider

    return factory


# -- generation ---------------------------------------------------------------

def test_files_are_written_to_the_workspace(scripted, tmp_path: Path):
    scripted([GOOD_SUBMISSION])
    pipeline = Pipeline(
        llm_config=LLMConfig(provider="scripted"),
        gate_config=GateConfig(sandbox="local"),
    )
    result = pipeline.build("convert celsius to fahrenheit",
                            tmp_path / "ws", verify=False)

    assert (tmp_path / "ws" / "temperature.py").exists()
    assert (tmp_path / "ws" / "tests" / "test_temperature.py").exists()
    assert result.submission is not None
    assert result.report is None  # verify=False


def test_scaffolding_is_added_so_imports_resolve(scripted, tmp_path: Path):
    scripted([GOOD_SUBMISSION])
    pipeline = Pipeline(llm_config=LLMConfig(provider="scripted"),
                        gate_config=GateConfig(sandbox="local"))
    pipeline.build("x", tmp_path / "ws", verify=False)

    # The agent is not asked to produce these, so a correct submission must
    # not fail on an import-path technicality.
    assert (tmp_path / "ws" / "conftest.py").exists()
    assert (tmp_path / "ws" / "pytest.ini").exists()


def test_agent_supplied_scaffolding_is_not_overwritten(scripted, tmp_path: Path):
    submission = json.loads(GOOD_SUBMISSION)
    submission["files"].append(
        {"path": "conftest.py", "content": "# agent's own conftest\n"}
    )
    scripted([json.dumps(submission)])
    pipeline = Pipeline(llm_config=LLMConfig(provider="scripted"),
                        gate_config=GateConfig(sandbox="local"))
    pipeline.build("x", tmp_path / "ws", verify=False)

    assert "agent's own conftest" in \
        (tmp_path / "ws" / "conftest.py").read_text(encoding="utf-8")


def test_agent_failure_yields_a_result_not_an_exception(scripted, tmp_path: Path):
    # One response per agent repair attempt, all of them invalid.
    scripted(["not json", "still not json", "nope"])
    pipeline = Pipeline(
        llm_config=LLMConfig(provider="scripted", max_retries=1,
                             backoff_base_s=0.0),
        gate_config=GateConfig(sandbox="local"),
    )
    result = pipeline.build("x", tmp_path / "ws", verify=False)

    assert result.failure is not None
    assert result.report is None
    assert result.verdict == "FAILED TO BUILD"
    assert result.passed is False


# -- verification -------------------------------------------------------------

@pytest.mark.slow
def test_good_submission_passes_the_fast_gates(scripted, tmp_path: Path):
    scripted([GOOD_SUBMISSION])
    config = GateConfig(sandbox="local", disabled=("G3", "G4", "G6"))
    pipeline = Pipeline(llm_config=LLMConfig(provider="scripted"),
                        gate_config=config)
    result = pipeline.build("convert celsius to fahrenheit", tmp_path / "ws")

    assert result.report is not None
    by_id = {r.gate_id: r for r in result.report.results}
    assert by_id["G1"].status is Status.PASS
    assert by_id["G2"].status is Status.PASS
    assert by_id["G5"].status is Status.PASS


@pytest.mark.slow
def test_insecure_submission_is_caught_despite_passing_its_own_tests(
    scripted, tmp_path: Path
):
    """The central claim, end to end: green tests are not enough."""
    scripted([INSECURE_SUBMISSION])
    config = GateConfig(sandbox="local", disabled=("G3", "G4", "G6"))
    pipeline = Pipeline(llm_config=LLMConfig(provider="scripted"),
                        gate_config=config)
    result = pipeline.build("run a rule expression", tmp_path / "ws")

    assert result.report is not None
    by_id = {r.gate_id: r for r in result.report.results}
    assert by_id["G2"].status is Status.PASS, "its own tests pass"
    assert by_id["G5"].status is Status.FAIL, "but the security gate blocks it"
    assert result.passed is False
    assert result.verdict == "UNVERIFIED"

    codes = " ".join(f.code for f in by_id["G5"].findings)
    assert "secret" in codes
    assert "bandit" in codes


# -- the no-self-grading rule -------------------------------------------------

def test_agent_self_assessment_never_affects_the_verdict(scripted, tmp_path: Path):
    submission = json.loads(GOOD_SUBMISSION)
    submission["self_assessment"] = "This code is fully verified and secure."
    scripted([json.dumps(submission)])

    pipeline = Pipeline(llm_config=LLMConfig(provider="scripted"),
                        gate_config=GateConfig(sandbox="local"))
    result = pipeline.build("x", tmp_path / "ws", verify=False)

    # The claim is retained for the report, but it is not a result and the
    # verdict does not come from it.
    assert result.unverified_claim() == "This code is fully verified and secure."
    assert result.verdict == "FAILED TO BUILD" or result.report is None


def test_path_escape_is_refused_at_write_time(scripted, tmp_path: Path):
    """Defence in depth: the contract rejects it, and so does the writer."""
    from codecrew.agents.contracts import CodeSubmission, SourceFile

    submission = CodeSubmission(files=[SourceFile("ok.py", "x = 1\n")])
    submission.files.append(SourceFile("../escaped.py", "x = 1\n"))

    pipeline = Pipeline(llm_config=LLMConfig(provider="scripted"),
                        gate_config=GateConfig(sandbox="local"))
    with pytest.raises(ValueError, match="outside the workspace"):
        pipeline._materialise(tmp_path / "ws", submission)


# -- tracing and cost ---------------------------------------------------------

def test_run_is_traced_from_start_to_end(scripted, tmp_path: Path):
    scripted([GOOD_SUBMISSION])
    pipeline = Pipeline(llm_config=LLMConfig(provider="scripted"),
                        gate_config=GateConfig(sandbox="local"))
    result = pipeline.build("x", tmp_path / "ws", verify=False)

    trace_dir = tmp_path / "ws" / ".codecrew" / "traces"
    files = list(trace_dir.glob("*.jsonl"))
    assert files, "a trace file must be written"
    kinds = [json.loads(line)["kind"]
             for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert kinds[0] == "run_start"
    assert "files_written" in kinds
    assert kinds[-1] == "run_end"
    assert result.cost["llm_calls"] == 1
