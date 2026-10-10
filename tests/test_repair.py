"""The reconciliation loop: diagnosis contracts, patch guards, and the
accept/revert decision.

The loop is driven here by a scripted model and a stub verifier that reads
the real files on disk, so what is asserted is the loop's own logic - which
artefact it touches and when it undoes a change - with no model quality and
no gate timing involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codecrew.agents import (
    CodeSubmission,
    ContractError,
    Diagnosis,
    SourceFile,
    Spec,
)
from codecrew.agents.contracts import (
    CODE_DEFECT,
    SPEC_DEFECT,
    TEST_DEFECT,
    count_test_functions,
    parse_patch,
    parse_spec_repair,
)
from codecrew.llm import LLMClient, LLMConfig, ScriptedProvider
from codecrew.repair import (
    ACCEPTED,
    AGENT_FAILED,
    REFUSED,
    REVERTED,
    RepairLoop,
    describe_failure,
    progress_key,
)
from codecrew.results import (
    Finding,
    GateReport,
    GateResult,
    Severity,
    Status,
)
from codecrew.trace import NullTracer

SPEC = {
    "summary": "Clamp a number into an inclusive range.",
    "module": "clamp",
    "functions": [{
        "signature": "clamp(value: float, low: float, high: float) -> float",
        "description": "Limit value to [low, high]; low > high raises ValueError.",
        "raises": ["ValueError: when low > high"],
        "examples": [
            {"call": "clamp(5, 0, 10)", "result": "5"},
            {"call": "clamp(1, 5, 0)", "raises": "ValueError"},
        ],
    }],
    "security": ["none"],
    "assumptions": [],
}

IMPL = '''
def clamp(value, low, high):
    if low > high:
        raise ValueError("bad range")
    return max(low, min(value, high))
'''

BAD_TESTS = '''
import pytest
from clamp import clamp


def test_inside():
    assert clamp(5, 0, 10) == 6  # WRONG


def test_inverted_raises():
    with pytest.raises(ValueError):
        clamp(1, 5, 0)
'''

GOOD_TESTS = BAD_TESTS.replace("== 6  # WRONG", "== 5")


def result(gate_id: str, status: Status, *, tests_failing: int = 0,
           evidence: str | None = None) -> GateResult:
    findings = [Finding(Severity.HIGH, "test-failure", f"t{i} failed",
                        "tests/test_clamp.py") for i in range(tests_failing)]
    return GateResult(gate_id=gate_id, name=gate_id, status=status,
                      summary=status.value, findings=findings, blocking=True,
                      evidence=evidence)


def report(**statuses) -> GateReport:
    failing = statuses.pop("failing", 0)
    results = [result(g, s, tests_failing=failing if g == "G2" else 0)
               for g, s in statuses.items()]
    return GateReport(run_id="t", workspace="w", sandbox="stub", results=results)


def diagnosis_json(verdict: str, targets=("tests/test_clamp.py",)) -> str:
    return json.dumps({
        "verdict": verdict, "target_files": list(targets),
        "root_cause": "the test expects 6", "evidence": "clamp(5, 0, 10) -> 5",
        "fix_instruction": "expect 5",
    })


def patch_json(path: str, content: str) -> str:
    return json.dumps({"files": [{"path": path, "content": content}],
                       "notes": "fixed"})


def make_loop(tmp_path: Path, responses, verify, approve=None, rounds=2):
    provider = ScriptedProvider(list(responses))
    client = LLMClient(LLMConfig(provider="scripted", max_retries=1,
                                 backoff_base_s=0.0), provider=provider)
    return RepairLoop(client, NullTracer(), tmp_path, verify,
                      approve=approve, max_rounds=rounds), provider


@pytest.fixture
def workspace(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "clamp.py").write_text(IMPL.lstrip("\n"), encoding="utf-8")
    (tmp_path / "tests" / "test_clamp.py").write_text(
        BAD_TESTS.lstrip("\n"), encoding="utf-8")
    (tmp_path / ".codecrew").mkdir()
    (tmp_path / ".codecrew" / "spec.json").write_text(
        Spec.from_dict(SPEC).to_json(), encoding="utf-8")
    submission = CodeSubmission(files=[
        SourceFile("clamp.py", IMPL.lstrip("\n")),
        SourceFile("tests/test_clamp.py", BAD_TESTS.lstrip("\n")),
    ])
    return tmp_path, submission, Spec.from_dict(SPEC)


def disk_verifier(ws: Path):
    """Passes once the wrong expectation is gone from the test file."""
    def verify(label: str) -> GateReport:
        text = (ws / "tests" / "test_clamp.py").read_text(encoding="utf-8")
        if "WRONG" in text:
            return report(G1=Status.PASS, G2=Status.FAIL, failing=1)
        return report(G1=Status.PASS, G2=Status.PASS)
    return verify


# -- the diagnosis contract ---------------------------------------------------

FILES = {"clamp.py": IMPL, "tests/test_clamp.py": BAD_TESTS}


def test_a_valid_test_defect_diagnosis_parses():
    diag = Diagnosis.parse(diagnosis_json(TEST_DEFECT), files=FILES)
    assert diag.verdict == TEST_DEFECT
    assert diag.target_files == ("tests/test_clamp.py",)


def test_code_defect_must_target_an_implementation_file():
    with pytest.raises(ContractError, match="implementation files"):
        Diagnosis.parse(diagnosis_json(CODE_DEFECT, ["tests/test_clamp.py"]),
                        files=FILES)


def test_test_defect_must_target_a_test_file():
    with pytest.raises(ContractError, match="test files"):
        Diagnosis.parse(diagnosis_json(TEST_DEFECT, ["clamp.py"]), files=FILES)


def test_spec_defect_needs_no_files_and_ignores_any_given():
    diag = Diagnosis.parse(diagnosis_json(SPEC_DEFECT, ["clamp.py"]), files=FILES)
    assert diag.target_files == ()


def test_unknown_target_file_is_rejected_with_the_known_ones():
    with pytest.raises(ContractError) as exc:
        Diagnosis.parse(diagnosis_json(CODE_DEFECT, ["ghost.py"]), files=FILES)
    assert "unknown file" in exc.value.message
    assert "clamp.py" in (exc.value.hint or "")


def test_a_verdict_outside_the_three_is_rejected():
    bad = json.loads(diagnosis_json(CODE_DEFECT, ["clamp.py"]))
    bad["verdict"] = "everything_is_fine"
    with pytest.raises(ContractError, match="verdict"):
        Diagnosis.parse(json.dumps(bad), files=FILES)


# -- patch guards -------------------------------------------------------------

def test_patch_may_only_touch_files_it_was_given():
    text = json.dumps({"files": [
        {"path": "tests/test_clamp.py", "content": GOOD_TESTS},
        {"path": "clamp.py", "content": IMPL}]})
    with pytest.raises(ContractError, match="outside its brief"):
        parse_patch(text, allowed={"tests/test_clamp.py"},
                    before={"tests/test_clamp.py": BAD_TESTS})


def test_patch_must_return_every_file_it_was_asked_to_fix():
    text = patch_json("a.py", "x = 1\n")
    with pytest.raises(ContractError, match="omits"):
        parse_patch(text, allowed={"a.py", "b.py"}, before={})


def test_deleting_tests_to_go_green_is_rejected():
    """The cheapest route to a green verdict is deleting the evidence."""
    only_one = ("import pytest\nfrom clamp import clamp\n\n"
                "def test_inside():\n    assert clamp(5, 0, 10) == 5\n")
    assert count_test_functions(BAD_TESTS) == 2
    with pytest.raises(ContractError) as exc:
        parse_patch(patch_json("tests/test_clamp.py", only_one),
                    allowed={"tests/test_clamp.py"},
                    before={"tests/test_clamp.py": BAD_TESTS},
                    max_test_removals=0)
    assert "removes 1 test function" in exc.value.message
    assert "Correct" in (exc.value.hint or "")


def test_correcting_an_expectation_in_place_is_accepted():
    patched = parse_patch(patch_json("tests/test_clamp.py", GOOD_TESTS),
                          allowed={"tests/test_clamp.py"},
                          before={"tests/test_clamp.py": BAD_TESTS})
    assert "WRONG" not in patched["tests/test_clamp.py"]


def test_adding_tests_is_always_allowed():
    more = GOOD_TESTS + "\n\ndef test_extra():\n    assert clamp(0, 0, 1) == 0\n"
    assert parse_patch(patch_json("tests/test_clamp.py", more),
                       allowed={"tests/test_clamp.py"},
                       before={"tests/test_clamp.py": BAD_TESTS})


def test_a_truncated_patch_is_rejected():
    with pytest.raises(ContractError, match="not valid Python"):
        parse_patch(patch_json("clamp.py", "def clamp(a, b):\n    return a +"),
                    allowed={"clamp.py"}, before={"clamp.py": IMPL})


# -- spec repair --------------------------------------------------------------

def test_spec_repair_may_fix_examples_but_not_rename_the_api():
    original = Spec.from_dict(SPEC)
    renamed = json.loads(json.dumps(SPEC))
    renamed["module"] = "limiter"
    with pytest.raises(ContractError, match="module name changed"):
        parse_spec_repair(json.dumps(renamed), original=original)

    changed = json.loads(json.dumps(SPEC))
    changed["functions"][0]["signature"] = "bound(value, low, high)"
    with pytest.raises(ContractError, match="set of functions changed"):
        parse_spec_repair(json.dumps(changed), original=original)

    fixed = json.loads(json.dumps(SPEC))
    fixed["functions"][0]["examples"][0]["result"] = "5.0"
    assert parse_spec_repair(json.dumps(fixed), original=original)


# -- progress measure ---------------------------------------------------------

def test_unmasked_failures_count_as_progress_not_regression():
    """Fixing G2 lets G3 run and fail; that is progress being revealed."""
    before = report(G1=Status.PASS, G2=Status.FAIL, G3=Status.SKIP, failing=2)
    after = report(G1=Status.PASS, G2=Status.PASS, G3=Status.FAIL)
    assert progress_key(after) > progress_key(before)


def test_fewer_failing_tests_is_progress_at_equal_pass_count():
    assert progress_key(report(G1=Status.PASS, G2=Status.FAIL, failing=1)) > \
        progress_key(report(G1=Status.PASS, G2=Status.FAIL, failing=3))


def test_failure_description_carries_located_findings(tmp_path: Path):
    text = describe_failure(report(G1=Status.PASS, G2=Status.FAIL, failing=2),
                            tmp_path)
    assert "G2" in text and "test-failure" in text
    assert "tests/test_clamp.py" in text
    assert text.startswith("G2"), "only blocked gates are described"


# -- the loop: accept ---------------------------------------------------------

def test_a_wrong_test_is_fixed_in_the_test_not_the_code(workspace):
    ws, submission, spec = workspace
    loop, provider = make_loop(ws, [
        diagnosis_json(TEST_DEFECT),
        patch_json("tests/test_clamp.py", GOOD_TESTS),
    ], disk_verifier(ws))

    start = report(G1=Status.PASS, G2=Status.FAIL, failing=1)
    outcome = loop.run(spec, submission, start)

    assert outcome.resolved is True
    assert [s.outcome for s in outcome.steps] == [ACCEPTED]
    assert outcome.steps[0].verdict == TEST_DEFECT
    assert "WRONG" not in (ws / "tests" / "test_clamp.py").read_text("utf-8")
    # The implementation was correct and must be untouched, byte for byte.
    assert (ws / "clamp.py").read_text("utf-8") == IMPL.lstrip("\n")


def test_the_diagnostician_is_shown_spec_code_tests_and_the_failure(workspace):
    ws, submission, spec = workspace
    loop, provider = make_loop(ws, [
        diagnosis_json(TEST_DEFECT),
        patch_json("tests/test_clamp.py", GOOD_TESTS),
    ], disk_verifier(ws))
    loop.run(spec, submission, report(G1=Status.PASS, G2=Status.FAIL, failing=1))

    prompt = "\n".join(m.content for m in provider.calls[0])
    assert '"module": "clamp"' in prompt            # the spec
    assert "def clamp(" in prompt                   # the code
    assert "# WRONG" in prompt                      # the tests
    assert "test-failure" in prompt                 # the failure evidence


# -- the loop: revert ---------------------------------------------------------

def test_a_patch_that_breaks_a_passing_gate_is_reverted(workspace):
    """Fixes the failing test but introduces a security regression."""
    ws, submission, spec = workspace

    def verify(label: str) -> GateReport:
        return report(G1=Status.PASS, G2=Status.PASS, G5=Status.FAIL)

    loop, _ = make_loop(ws, [
        diagnosis_json(TEST_DEFECT),
        patch_json("tests/test_clamp.py", GOOD_TESTS),
    ], verify, rounds=1)

    start = report(G1=Status.PASS, G2=Status.FAIL, G5=Status.PASS, failing=1)
    outcome = loop.run(spec, submission, start)

    assert outcome.resolved is False
    assert outcome.steps[0].outcome == REVERTED
    assert "regressed G5" in outcome.steps[0].note
    # Disk is back to the pre-patch content.
    assert "# WRONG" in (ws / "tests" / "test_clamp.py").read_text("utf-8")


def test_a_patch_with_no_progress_is_reverted(workspace):
    ws, submission, spec = workspace
    unchanged = lambda label: report(G1=Status.PASS, G2=Status.FAIL, failing=1)
    loop, _ = make_loop(ws, [
        diagnosis_json(TEST_DEFECT),
        patch_json("tests/test_clamp.py", GOOD_TESTS),
    ], unchanged, rounds=1)

    outcome = loop.run(spec, submission,
                       report(G1=Status.PASS, G2=Status.FAIL, failing=1))
    assert outcome.steps[0].outcome == REVERTED
    assert "no progress" in outcome.steps[0].note
    assert "# WRONG" in (ws / "tests" / "test_clamp.py").read_text("utf-8")


def test_failed_attempts_are_fed_back_as_negative_examples(workspace):
    ws, submission, spec = workspace
    stuck = lambda label: report(G1=Status.PASS, G2=Status.FAIL, failing=1)
    loop, provider = make_loop(ws, [
        diagnosis_json(TEST_DEFECT), patch_json("tests/test_clamp.py", GOOD_TESTS),
        diagnosis_json(TEST_DEFECT), patch_json("tests/test_clamp.py", GOOD_TESTS),
    ], stuck, rounds=2)
    loop.run(spec, submission, report(G1=Status.PASS, G2=Status.FAIL, failing=1))

    first_patch = "\n".join(m.content for m in provider.calls[1])
    second_patch = "\n".join(m.content for m in provider.calls[3])
    assert "(none)" in first_patch
    assert "reverted" in second_patch


# -- the loop: agent failure and refusal ---------------------------------------

THREE_TESTS = BAD_TESTS + (
    "\n\ndef test_below():\n    assert clamp(-1, 0, 10) == 0\n")


def test_a_patch_that_deletes_more_tests_than_failed_is_refused(workspace):
    """One test failed, so one may go; deleting two is the cheap way to green."""
    ws, _, spec = workspace
    (ws / "tests" / "test_clamp.py").write_text(
        THREE_TESTS.lstrip("\n"), encoding="utf-8")
    submission = CodeSubmission(files=[
        SourceFile("clamp.py", IMPL.lstrip("\n")),
        SourceFile("tests/test_clamp.py", THREE_TESTS.lstrip("\n")),
    ])
    leaves_one = ("from clamp import clamp\n\n"
                  "def test_inside():\n    assert clamp(5, 0, 10) == 5\n")
    loop, _ = make_loop(ws, [
        diagnosis_json(TEST_DEFECT),
        *[patch_json("tests/test_clamp.py", leaves_one)] * 3,
    ], disk_verifier(ws), rounds=1)

    outcome = loop.run(spec, submission,
                       report(G1=Status.PASS, G2=Status.FAIL, failing=1))
    assert outcome.steps[0].outcome == AGENT_FAILED
    assert "# WRONG" in (ws / "tests" / "test_clamp.py").read_text("utf-8")


def test_removing_exactly_the_diagnosed_failing_test_is_allowed(workspace):
    """A test of unspecified behaviour has no correct form; removal is the fix."""
    ws, _, spec = workspace
    (ws / "tests" / "test_clamp.py").write_text(
        THREE_TESTS.lstrip("\n"), encoding="utf-8")
    submission = CodeSubmission(files=[
        SourceFile("clamp.py", IMPL.lstrip("\n")),
        SourceFile("tests/test_clamp.py", THREE_TESTS.lstrip("\n")),
    ])
    without_the_wrong_one = (
        "import pytest\nfrom clamp import clamp\n\n"
        "def test_inverted_raises():\n    with pytest.raises(ValueError):\n"
        "        clamp(1, 5, 0)\n\n"
        "def test_below():\n    assert clamp(-1, 0, 10) == 0\n")
    loop, _ = make_loop(ws, [
        diagnosis_json(TEST_DEFECT),
        patch_json("tests/test_clamp.py", without_the_wrong_one),
    ], disk_verifier(ws), rounds=1)

    outcome = loop.run(spec, submission,
                       report(G1=Status.PASS, G2=Status.FAIL, failing=1))
    assert outcome.steps[0].outcome == ACCEPTED


def test_an_undiagnosable_failure_is_recorded_not_papered_over(workspace):
    ws, submission, spec = workspace
    loop, _ = make_loop(ws, ["nonsense", "nonsense", "nonsense"],
                        disk_verifier(ws), rounds=1)
    outcome = loop.run(spec, submission,
                       report(G1=Status.PASS, G2=Status.FAIL, failing=1))
    assert outcome.resolved is False
    assert outcome.steps[0].outcome == AGENT_FAILED
    assert outcome.steps[0].verdict is None


def test_an_already_passing_report_is_left_alone(workspace):
    ws, submission, spec = workspace
    loop, provider = make_loop(ws, [], disk_verifier(ws))
    outcome = loop.run(spec, submission, report(G1=Status.PASS, G2=Status.PASS))
    assert outcome.resolved and outcome.steps == []
    assert provider.calls == [], "no model call is made when nothing failed"


# -- the loop: spec defect -----------------------------------------------------

CORRECTED = json.loads(json.dumps(SPEC))
CORRECTED["summary"] = "CORRECTED-MARKER clamp a number into a range."


def spec_verifier(ws: Path):
    def verify(label: str) -> GateReport:
        text = (ws / ".codecrew" / "spec.json").read_text(encoding="utf-8")
        if "CORRECTED-MARKER" in text:
            return report(G1=Status.PASS, G2=Status.PASS)
        return report(G1=Status.PASS, G2=Status.FAIL, failing=1)
    return verify


def test_a_spec_defect_corrects_the_spec_and_rederives_the_tests(workspace):
    ws, submission, spec = workspace
    asked = []

    def approve(new_spec):
        asked.append(new_spec)
        return new_spec

    loop, provider = make_loop(ws, [
        diagnosis_json(SPEC_DEFECT, []),
        json.dumps(CORRECTED),
        patch_json("tests/test_clamp.py", GOOD_TESTS),
    ], spec_verifier(ws), approve=approve)

    outcome = loop.run(spec, submission,
                       report(G1=Status.PASS, G2=Status.FAIL, failing=1))

    assert outcome.resolved is True
    assert outcome.steps[0].verdict == SPEC_DEFECT
    assert "CORRECTED-MARKER" in outcome.spec.summary
    assert "CORRECTED-MARKER" in (ws / ".codecrew" / "spec.json").read_text("utf-8")
    assert len(asked) == 1, "the human sees the corrected spec"
    assert "WRONG" not in (ws / "tests" / "test_clamp.py").read_text("utf-8")
    # The implementation is not regenerated by a spec correction.
    assert (ws / "clamp.py").read_text("utf-8") == IMPL.lstrip("\n")


def test_the_test_author_stays_blind_to_the_code_during_spec_repair(workspace):
    ws, submission, spec = workspace
    loop, provider = make_loop(ws, [
        diagnosis_json(SPEC_DEFECT, []),
        json.dumps(CORRECTED),
        patch_json("tests/test_clamp.py", GOOD_TESTS),
    ], spec_verifier(ws))
    loop.run(spec, submission, report(G1=Status.PASS, G2=Status.FAIL, failing=1))

    # Calls: diagnostician, spec repair, test author.
    author_prompt = "\n".join(m.content for m in provider.calls[2])
    assert "def clamp(" not in author_prompt
    assert "CORRECTED-MARKER" in author_prompt


def test_a_rejected_corrected_spec_stops_the_loop_without_changes(workspace):
    ws, submission, spec = workspace
    loop, _ = make_loop(ws, [
        diagnosis_json(SPEC_DEFECT, []),
        json.dumps(CORRECTED),
    ], spec_verifier(ws), approve=lambda s: None)

    outcome = loop.run(spec, submission,
                       report(G1=Status.PASS, G2=Status.FAIL, failing=1))

    assert outcome.steps[0].outcome == REFUSED
    assert "CORRECTED-MARKER" not in \
        (ws / ".codecrew" / "spec.json").read_text("utf-8")
    assert "# WRONG" in (ws / "tests" / "test_clamp.py").read_text("utf-8")


def test_a_reverted_spec_repair_restores_spec_and_tests(workspace):
    ws, submission, spec = workspace
    stuck = lambda label: report(G1=Status.PASS, G2=Status.FAIL, failing=1)
    loop, _ = make_loop(ws, [
        diagnosis_json(SPEC_DEFECT, []),
        json.dumps(CORRECTED),
        patch_json("tests/test_clamp.py", GOOD_TESTS),
    ], stuck, rounds=1)

    outcome = loop.run(spec, submission,
                       report(G1=Status.PASS, G2=Status.FAIL, failing=1))

    assert outcome.steps[0].outcome == REVERTED
    assert outcome.spec.summary == SPEC["summary"]
    assert "CORRECTED-MARKER" not in \
        (ws / ".codecrew" / "spec.json").read_text("utf-8")
    assert "# WRONG" in (ws / "tests" / "test_clamp.py").read_text("utf-8")
