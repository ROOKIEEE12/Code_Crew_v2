"""Contracts for the Phase 2 agents: the spec, the implementation-only
submission and the tests-only submission."""

from __future__ import annotations

import json

import pytest

from codecrew.agents import CodeSubmission, ContractError, Spec

GOOD_SPEC = {
    "summary": "Clamp a number into an inclusive range.",
    "module": "clamp",
    "functions": [{
        "signature": "clamp(value: float, low: float, high: float) -> float",
        "description": "Return value limited to [low, high]. low > high is an error.",
        "raises": ["ValueError: when low > high"],
        "examples": [
            {"call": "clamp(5, 0, 10)", "result": "5"},
            {"call": "clamp(-1, 0, 10)", "result": "0"},
            {"call": "clamp(1, 5, 0)", "raises": "ValueError"},
        ],
    }],
    "constraints": ["pure function"],
    "security": ["no untrusted input"],
    "assumptions": ["ints and floats both accepted"],
    "acceptance_criteria": ["boundaries are inclusive"],
}


def spec_with(**overrides) -> str:
    data = json.loads(json.dumps(GOOD_SPEC))
    data.update(overrides)
    return json.dumps(data)


# -- Spec ---------------------------------------------------------------------

def test_valid_spec_parses_and_round_trips():
    spec = Spec.parse(json.dumps(GOOD_SPEC))
    assert spec.module == "clamp"
    assert spec.functions[0].name == "clamp"
    assert len(spec.functions[0].examples) == 3
    assert Spec.parse(spec.to_json()).to_dict() == spec.to_dict()


def test_spec_is_accepted_inside_markdown_fences():
    text = "Here you go:\n```json\n" + json.dumps(GOOD_SPEC) + "\n```"
    assert Spec.parse(text).module == "clamp"


def test_render_surfaces_assumptions_for_the_reviewer():
    rendered = Spec.parse(json.dumps(GOOD_SPEC)).render()
    assert "Assumptions (CHECK THESE)" in rendered
    assert "ints and floats both accepted" in rendered
    assert "clamp(5, 0, 10) -> 5" in rendered
    assert "raises ValueError" in rendered


@pytest.mark.parametrize("module", ["Clamp", "my-module", "1abc", "class", "", None, 3])
def test_bad_module_names_are_rejected(module):
    with pytest.raises(ContractError, match="module"):
        Spec.parse(spec_with(module=module))


def test_missing_summary_is_rejected():
    with pytest.raises(ContractError, match="summary"):
        Spec.parse(spec_with(summary=""))


def test_empty_functions_are_rejected():
    with pytest.raises(ContractError, match="functions"):
        Spec.parse(spec_with(functions=[]))


def test_invalid_signature_is_rejected_with_the_expected_form():
    bad = json.loads(json.dumps(GOOD_SPEC))
    bad["functions"][0]["signature"] = "clamp value low high"
    with pytest.raises(ContractError) as exc:
        Spec.parse(json.dumps(bad))
    assert "not a valid Python signature" in exc.value.message
    assert "name(arg: type" in (exc.value.hint or "")


def test_def_prefix_in_a_signature_is_tolerated():
    data = json.loads(json.dumps(GOOD_SPEC))
    data["functions"][0]["signature"] = "def clamp(value, low, high)"
    assert Spec.parse(json.dumps(data)).functions[0].name == "clamp"


def test_duplicate_functions_are_rejected():
    data = json.loads(json.dumps(GOOD_SPEC))
    data["functions"].append(data["functions"][0])
    with pytest.raises(ContractError, match="twice"):
        Spec.parse(json.dumps(data))


def test_example_needs_exactly_one_of_result_or_raises():
    data = json.loads(json.dumps(GOOD_SPEC))
    data["functions"][0]["examples"] = [{"call": "clamp(1, 0, 2)"}]
    with pytest.raises(ContractError, match="exactly one of"):
        Spec.parse(json.dumps(data))

    data["functions"][0]["examples"] = [
        {"call": "clamp(1, 0, 2)", "result": "1", "raises": "ValueError"}]
    with pytest.raises(ContractError, match="exactly one of"):
        Spec.parse(json.dumps(data))


def test_example_expressions_must_be_valid_python():
    data = json.loads(json.dumps(GOOD_SPEC))
    data["functions"][0]["examples"] = [{"call": "clamp(1, ", "result": "1"}]
    with pytest.raises(ContractError, match="not a valid Python expression"):
        Spec.parse(json.dumps(data))


def test_raises_must_be_an_exception_name():
    data = json.loads(json.dumps(GOOD_SPEC))
    data["functions"][0]["examples"] = [
        {"call": "clamp(1, 5, 0)", "raises": "not a name"}]
    with pytest.raises(ContractError, match="exception class name"):
        Spec.parse(json.dumps(data))


def test_string_lists_reject_non_strings():
    with pytest.raises(ContractError, match="array of strings"):
        Spec.parse(spec_with(assumptions=[1, 2]))


# -- implementation-only submissions ------------------------------------------

IMPL = "def clamp(value, low, high):\n    return max(low, min(value, high))\n"
TESTS = ("from clamp import clamp\n\n"
         "def test_inside():\n    assert clamp(5, 0, 10) == 5\n")


def files(*pairs) -> str:
    return json.dumps({"files": [{"path": p, "content": c} for p, c in pairs]})


def test_implementation_accepts_the_named_module():
    sub = CodeSubmission.parse_implementation(files(("clamp.py", IMPL)),
                                              module="clamp")
    assert [f.path for f in sub.files] == ["clamp.py"]


def test_implementation_must_not_include_tests():
    """An implementer that writes tests reintroduces self-grading."""
    text = files(("clamp.py", IMPL), ("tests/test_clamp.py", TESTS))
    with pytest.raises(ContractError) as exc:
        CodeSubmission.parse_implementation(text, module="clamp")
    assert "test file" in exc.value.message
    assert "independently" in (exc.value.hint or "")


def test_implementation_must_define_the_specified_module_file():
    with pytest.raises(ContractError, match="clamp.py"):
        CodeSubmission.parse_implementation(files(("other.py", IMPL)),
                                            module="clamp")


def test_truncated_implementation_is_rejected():
    with pytest.raises(ContractError, match="not valid Python"):
        CodeSubmission.parse_implementation(
            files(("clamp.py", "def clamp(a, b):\n    return a +")),
            module="clamp")


# -- tests-only submissions ---------------------------------------------------

def test_tests_only_submission_is_accepted():
    sub = CodeSubmission.parse_tests(
        files(("tests/test_clamp.py", TESTS)), module="clamp")
    assert sub.test_files and not sub.source_files


def test_tests_must_not_smuggle_in_an_implementation():
    text = files(("tests/test_clamp.py", TESTS), ("clamp.py", IMPL))
    with pytest.raises(ContractError, match="non-test file"):
        CodeSubmission.parse_tests(text, module="clamp")


def test_tests_must_import_the_module_under_test():
    code = "def test_nothing():\n    assert 1 + 1 == 2\n"
    with pytest.raises(ContractError, match="never imports"):
        CodeSubmission.parse_tests(files(("tests/test_clamp.py", code)),
                                   module="clamp")


def test_tests_must_define_test_functions():
    code = "from clamp import clamp\n\nVALUE = clamp(1, 0, 2)\n"
    with pytest.raises(ContractError, match="no test_"):
        CodeSubmission.parse_tests(files(("tests/test_clamp.py", code)),
                                   module="clamp")


def test_tests_that_never_assert_are_rejected():
    """A test that cannot fail verifies nothing."""
    code = "from clamp import clamp\n\ndef test_runs():\n    clamp(1, 0, 2)\n"
    with pytest.raises(ContractError, match="no assertions"):
        CodeSubmission.parse_tests(files(("tests/test_clamp.py", code)),
                                   module="clamp")


def test_pytest_raises_counts_as_an_assertion():
    code = ("import pytest\nfrom clamp import clamp\n\n"
            "def test_err():\n    with pytest.raises(ValueError):\n"
            "        clamp(1, 5, 0)\n")
    assert CodeSubmission.parse_tests(files(("tests/test_clamp.py", code)),
                                      module="clamp").test_files
