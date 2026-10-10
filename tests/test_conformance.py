"""G7 (spec conformance), the surface-assertion rule, and the repair loop run
end to end through the real gates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codecrew.agents import CodeSubmission, ContractError
from codecrew.config import GateConfig
from codecrew.gates import GateRunner
from codecrew.llm import LLMConfig, ScriptedProvider
from codecrew.pipeline import Pipeline
from codecrew.results import Status

IMPL = '''
def clamp(value, low, high):
    if low > high:
        raise ValueError("bad range")
    return max(low, min(value, high))
'''

SPEC = {
    "summary": "Clamp a number into an inclusive range.",
    "module": "clamp",
    "functions": [{
        "signature": "clamp(value: float, low: float, high: float) -> float",
        "description": "Limit value to [low, high]; low > high raises ValueError.",
        "raises": ["ValueError: when low > high"],
        "examples": [
            {"call": "clamp(5, 0, 10)", "result": "5"},
            {"call": "clamp(-1, 0, 10)", "result": "0"},
            {"call": "clamp(1, 5, 0)", "raises": "ValueError"},
        ],
    }],
    "security": ["none"],
    "assumptions": [],
}


def workspace_with(tmp_path: Path, spec: dict | None, impl: str = IMPL) -> Path:
    (tmp_path / "clamp.py").write_text(impl.lstrip("\n"), encoding="utf-8")
    if spec is not None:
        (tmp_path / ".codecrew").mkdir(exist_ok=True)
        (tmp_path / ".codecrew" / "spec.json").write_text(
            json.dumps(spec), encoding="utf-8")
    return tmp_path


def run_g7(ws: Path):
    report = GateRunner(ws, GateConfig(sandbox="local")).run(only=["G7"])
    return report.results


# -- the gate -----------------------------------------------------------------

def test_g7_passes_when_every_spec_example_holds(tmp_path: Path):
    (g7,) = run_g7(workspace_with(tmp_path, SPEC))
    assert g7.status is Status.PASS
    assert g7.score == 100.0
    assert "3/3" in g7.summary


def test_g7_names_the_example_that_disagrees(tmp_path: Path):
    """The Phase 2 case: a spec example that contradicts its own rule."""
    bad = json.loads(json.dumps(SPEC))
    bad["functions"][0]["examples"][0]["result"] = "6"       # clamp(5,0,10) is 5
    (g7,) = run_g7(workspace_with(tmp_path, bad))

    assert g7.status is Status.FAIL
    assert "2/3" in g7.summary
    (finding,) = g7.findings
    assert finding.code == "spec-example-mismatch"
    assert "clamp(5, 0, 10)" in finding.message
    assert "6" in finding.message and "returned 5" in finding.message
    assert "rule text decides" in (g7.note or "")


def test_g7_catches_a_missing_error(tmp_path: Path):
    lenient = "def clamp(value, low, high):\n    return max(low, min(value, high))\n"
    (g7,) = run_g7(workspace_with(tmp_path, SPEC, impl=lenient))
    assert g7.status is Status.FAIL
    assert any("raise ValueError" in f.message for f in g7.findings)


def test_g7_accepts_a_subclass_of_the_named_exception(tmp_path: Path):
    custom = ("class RangeError(ValueError):\n    pass\n\n\n"
              "def clamp(value, low, high):\n"
              "    if low > high:\n        raise RangeError('bad')\n"
              "    return max(low, min(value, high))\n")
    (g7,) = run_g7(workspace_with(tmp_path, SPEC, impl=custom))
    assert g7.status is Status.PASS


def test_g7_reports_an_unimportable_module(tmp_path: Path):
    broken = "import definitely_not_installed_anywhere\n\ndef clamp(a, b, c):\n    return a\n"
    (g7,) = run_g7(workspace_with(tmp_path, SPEC, impl=broken))
    assert g7.status is Status.FAIL
    assert "cannot be imported" in g7.summary
    assert "definitely_not_installed_anywhere" in g7.findings[0].message


def test_g7_does_not_apply_without_a_spec(tmp_path: Path):
    """Single-agent runs have no spec. Not applicable is not "skipped"."""
    ws = workspace_with(tmp_path, None)
    assert run_g7(ws) == []


def test_not_applicable_does_not_downgrade_the_verdict(tmp_path: Path):
    ws = workspace_with(tmp_path, None)
    (ws / "tests").mkdir()
    (ws / "tests" / "test_clamp.py").write_text(
        "from clamp import clamp\n\ndef test_x():\n    assert clamp(5, 0, 10) == 5\n",
        encoding="utf-8")
    report = GateRunner(ws, GateConfig(sandbox="local")).run(only=["G1", "G7"])
    assert [r.gate_id for r in report.results] == ["G1"]
    assert report.verdict() == "VERIFIED"


def test_spec_examples_run_in_the_sandbox_not_the_test_process(tmp_path: Path):
    """The checker executes arbitrary code, so it must not run in-process."""
    evil = ("import os\nos.environ['CODECREW_LEAK'] = 'yes'\n\n"
            "def clamp(a, b, c):\n    return a\n")
    run_g7(workspace_with(tmp_path, SPEC, impl=evil))
    import os
    assert "CODECREW_LEAK" not in os.environ


# -- the surface-assertion rule -----------------------------------------------

def make_tests_payload(body: str) -> str:
    return json.dumps({"files": [{
        "path": "tests/test_clamp.py",
        "content": "from clamp import clamp\n\n" + body}]})


@pytest.mark.parametrize("body", [
    "import clamp as m\n\ndef test_api():\n    assert 'clamp' in dir(clamp)\n",
    "def test_api():\n    assert __all__ == ['clamp']\n",
    "import inspect, clamp\n\ndef test_api():\n    assert inspect.getmembers(clamp)\n",
])
def test_tests_that_assert_the_module_surface_are_rejected(body):
    with pytest.raises(ContractError) as exc:
        CodeSubmission.parse_tests(make_tests_payload(body), module="clamp")
    assert "public surface" in exc.value.message
    assert "not part of any specification" in (exc.value.hint or "")


def test_ordinary_use_of_dir_on_other_objects_is_not_flagged():
    body = ("def test_value():\n"
            "    result = clamp(1, 0, 2)\n"
            "    assert 'real' in dir(result)\n")
    assert CodeSubmission.parse_tests(make_tests_payload(body), module="clamp").test_files


# -- the whole loop, through the real gates -------------------------------------

GOOD_TESTS = ("import pytest\nfrom clamp import clamp\n\n"
              "def test_inside():\n    assert clamp(5, 0, 10) == 5\n\n"
              "def test_below():\n    assert clamp(-1, 0, 10) == 0\n\n"
              "def test_inverted():\n    with pytest.raises(ValueError):\n"
              "        clamp(1, 5, 0)\n")


@pytest.fixture
def scripted(monkeypatch):
    def factory(responses: list[str]) -> ScriptedProvider:
        provider = ScriptedProvider(responses)
        monkeypatch.setattr("codecrew.llm.client.select_provider",
                            lambda name="auto", script=None: provider)
        return provider
    return factory


@pytest.mark.slow
def test_a_wrong_spec_example_is_found_diagnosed_and_repaired(
    scripted, tmp_path: Path
):
    """The Phase 2 failure, resolved end to end with no human in the loop.

    The spec's first example is wrong. The implementation and tests are
    right, so G7 is the only gate that can see the disagreement. The loop
    must classify it as a spec defect, correct the spec (not the code), and
    show a before/after verdict.
    """
    wrong = json.loads(json.dumps(SPEC))
    wrong["functions"][0]["examples"][0]["result"] = "6"

    diagnosis = json.dumps({
        "verdict": "spec_defect", "target_files": [],
        "root_cause": "example clamp(5, 0, 10) -> 6 contradicts the rule text",
        "evidence": "value 5 lies inside [0, 10], so it is returned unchanged",
        "fix_instruction": "change the example result to 5",
    })
    scripted([
        json.dumps(wrong),
        json.dumps({"files": [{"path": "clamp.py",
                               "content": IMPL.lstrip("\n")}]}),
        json.dumps({"files": [{"path": "tests/test_clamp.py",
                               "content": GOOD_TESTS}]}),
        diagnosis,
        json.dumps(SPEC),
        json.dumps({"files": [{"path": "tests/test_clamp.py",
                               "content": GOOD_TESTS}]}),
    ])

    config = GateConfig(sandbox="local", disabled=("G3", "G4", "G6"))
    result = Pipeline(LLMConfig(provider="scripted"), config).build(
        "clamp a number", tmp_path / "ws", mode="multi", repair_rounds=2)

    assert result.initial_verdict == "UNVERIFIED"
    assert [s.verdict for s in result.repairs] == ["spec_defect"]
    assert [s.outcome for s in result.repairs] == ["accepted"]
    assert result.passed
    # The implementation was right all along and was never rewritten.
    assert (tmp_path / "ws" / "clamp.py").read_text("utf-8") == IMPL.lstrip("\n")
    assert result.spec is not None
    assert result.spec.functions[0].examples[0].result == "5"


@pytest.mark.slow
def test_repair_is_off_by_default_so_the_base_result_stays_pure(
    scripted, tmp_path: Path
):
    wrong = json.loads(json.dumps(SPEC))
    wrong["functions"][0]["examples"][0]["result"] = "6"
    provider = scripted([
        json.dumps(wrong),
        json.dumps({"files": [{"path": "clamp.py",
                               "content": IMPL.lstrip("\n")}]}),
        json.dumps({"files": [{"path": "tests/test_clamp.py",
                               "content": GOOD_TESTS}]}),
    ])
    config = GateConfig(sandbox="local", disabled=("G3", "G4", "G6"))
    result = Pipeline(LLMConfig(provider="scripted"), config).build(
        "clamp a number", tmp_path / "ws", mode="multi")

    assert result.repairs == []
    assert not result.passed
    assert len(provider.calls) == 3, "no diagnostician call was made"
