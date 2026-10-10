"""Tests for the agent output contract.

These encode the boundary rules: nothing malformed, oversized, truncated or
path-escaping may reach the filesystem or a downstream agent.
"""

from __future__ import annotations

import json

import pytest

from codecrew.agents.contracts import (
    MAX_FILES,
    CodeSubmission,
    ContractError,
    extract_json,
)

GOOD = {
    "files": [
        {"path": "app.py", "content": "def add(a: int, b: int) -> int:\n    return a + b\n"},
        {"path": "tests/test_app.py", "content": "from app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"},
    ],
    "notes": "straightforward",
    "self_assessment": "unsure about overflow",
}


def payload(**overrides) -> str:
    data = json.loads(json.dumps(GOOD))
    data.update(overrides)
    return json.dumps(data)


# -- happy path ---------------------------------------------------------------

def test_valid_submission_parses():
    submission = CodeSubmission.parse(payload())
    assert [f.path for f in submission.files] == ["app.py", "tests/test_app.py"]
    assert submission.notes == "straightforward"
    assert submission.summary() == "1 source file(s), 1 test file(s)"


def test_test_files_are_classified_by_path_and_name():
    submission = CodeSubmission.parse(payload())
    assert [f.path for f in submission.source_files] == ["app.py"]
    assert [f.path for f in submission.test_files] == ["tests/test_app.py"]


def test_self_assessment_is_kept_but_separate_from_files():
    submission = CodeSubmission.parse(payload())
    # It is retained for the report, where it is labelled unverified.
    assert submission.self_assessment == "unsure about overflow"


# -- tolerant JSON extraction -------------------------------------------------

def test_json_in_markdown_fences_is_accepted():
    text = f"Here is my solution:\n\n```json\n{payload()}\n```\n\nHope it helps!"
    assert CodeSubmission.parse(text).files[0].path == "app.py"


def test_json_with_surrounding_prose_is_accepted():
    text = f"Sure! {payload()} Let me know if you need changes."
    assert CodeSubmission.parse(text).files[0].path == "app.py"


def test_braces_inside_strings_do_not_break_extraction():
    data = {
        "files": [
            {"path": "app.py", "content": "def f():\n    return {'a': 1}\n"},
            {"path": "tests/test_app.py",
             "content": "from app import f\n\ndef test_f():\n    assert f() == {'a': 1}\n"},
        ],
    }
    text = f"prose before {json.dumps(data)} prose after"
    submission = CodeSubmission.parse(text)
    assert "{'a': 1}" in submission.files[0].content


def test_empty_response_is_rejected():
    with pytest.raises(ContractError, match="empty"):
        extract_json("   ")


def test_unparseable_response_is_rejected_with_guidance():
    with pytest.raises(ContractError) as exc:
        CodeSubmission.parse("I cannot do that.")
    assert "JSON" in exc.value.message
    assert "Return only a JSON object" in exc.value.feedback()


# -- structural rules ---------------------------------------------------------

def test_missing_files_key_is_rejected():
    with pytest.raises(ContractError, match="no 'files' key"):
        CodeSubmission.parse('{"notes": "oops"}')


def test_empty_files_list_is_rejected():
    with pytest.raises(ContractError, match="non-empty array"):
        CodeSubmission.parse('{"files": []}')


def test_missing_tests_are_rejected_by_default():
    text = json.dumps({"files": [GOOD["files"][0]]})
    with pytest.raises(ContractError, match="no test file"):
        CodeSubmission.parse(text)


def test_missing_tests_can_be_allowed_explicitly():
    """Phase 2 splits code and tests, so the rule must be togglable."""
    text = json.dumps({"files": [GOOD["files"][0]]})
    submission = CodeSubmission.parse(text, require_tests=False)
    assert submission.test_files == []


def test_tests_without_implementation_are_rejected():
    text = json.dumps({"files": [GOOD["files"][1]]})
    with pytest.raises(ContractError, match="no implementation file"):
        CodeSubmission.parse(text, require_tests=False)


def test_duplicate_paths_are_rejected():
    text = json.dumps({"files": [GOOD["files"][0], GOOD["files"][0],
                                 GOOD["files"][1]]})
    with pytest.raises(ContractError, match="appears twice"):
        CodeSubmission.parse(text)


def test_too_many_files_are_rejected():
    files = [
        {"path": f"m{i}.py", "content": "x = 1\n"} for i in range(MAX_FILES + 1)
    ]
    with pytest.raises(ContractError, match="limit"):
        CodeSubmission.parse(json.dumps({"files": files}))


def test_empty_content_is_rejected():
    text = json.dumps({"files": [{"path": "app.py", "content": "   "},
                                 GOOD["files"][1]]})
    with pytest.raises(ContractError, match="empty content"):
        CodeSubmission.parse(text)


# -- truncation detection -----------------------------------------------------

def test_truncated_python_is_rejected_as_a_contract_error():
    """Catching this here is far cheaper than a full gate run."""
    text = json.dumps({
        "files": [
            {"path": "app.py", "content": "def add(a, b):\n    return a +"},
            GOOD["files"][1],
        ]
    })
    with pytest.raises(ContractError) as exc:
        CodeSubmission.parse(text)
    assert "not valid Python" in exc.value.message
    assert "cut off" in (exc.value.hint or "")


def test_non_python_files_are_not_syntax_checked():
    text = json.dumps({
        "files": [
            GOOD["files"][0], GOOD["files"][1],
            {"path": "README.md", "content": "# Title\n\nnot python at all\n"},
        ]
    })
    assert len(CodeSubmission.parse(text).files) == 3


# -- path containment ---------------------------------------------------------

@pytest.mark.parametrize(
    "path, match",
    [
        ("/etc/passwd", "absolute"),
        ("C:/Windows/system32/x.py", "absolute"),
        ("../../escape.py", "escapes the workspace"),
        ("nested/../../escape.py", "escapes the workspace"),
        ("payload.sh", "unsupported extension"),
        ("app.exe", "unsupported extension"),
    ],
)
def test_dangerous_paths_are_rejected(path: str, match: str):
    text = json.dumps({"files": [{"path": path, "content": "x = 1\n"},
                                 GOOD["files"][1]]})
    with pytest.raises(ContractError, match=match):
        CodeSubmission.parse(text)


def test_windows_separators_are_normalised():
    text = json.dumps({
        "files": [
            GOOD["files"][0],
            {"path": "tests\\test_app.py",
             "content": "from app import add\n\ndef test_add():\n    assert add(1, 1) == 2\n"},
        ]
    })
    submission = CodeSubmission.parse(text)
    assert submission.files[1].path == "tests/test_app.py"


def test_feedback_names_the_rule_and_asks_for_the_format():
    error = ContractError("'files' must be a non-empty array of objects",
                          hint="Expected shape: ...")
    feedback = error.feedback()
    assert "rejected" in feedback
    assert "Expected shape" in feedback
    assert "JSON" in feedback
