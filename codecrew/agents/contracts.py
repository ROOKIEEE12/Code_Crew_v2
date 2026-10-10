"""Typed contracts for agent output.

A large share of multi-agent failures are not reasoning failures but
hand-off failures: malformed JSON, a missing field, a path that escapes the
workspace. Those must be caught at the boundary and sent back to the agent
as a specific, actionable error - never passed downstream as garbage.

Validation is implemented against the standard library rather than a
schema framework so that CodeCrew installs with nothing but Python. The
error messages are written to be read by a model, not only by a human.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

#: Phase 1 is Python-only; the Architect lifts this in Phase 4.
ALLOWED_SUFFIXES = {".py", ".txt", ".md", ".ini", ".cfg", ".toml"}
MAX_FILES = 12
MAX_FILE_BYTES = 120_000


class ContractError(ValueError):
    """Agent output did not satisfy its contract.

    ``feedback`` is what gets appended to the conversation on retry, so it
    states the rule that was broken and what to do instead.
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def feedback(self) -> str:
        parts = [f"Your previous response was rejected: {self.message}"]
        if self.hint:
            parts.append(self.hint)
        parts.append(
            "Reply with corrected output in exactly the required JSON format, "
            "and nothing else."
        )
        return "\n\n".join(parts)


@dataclass(frozen=True)
class SourceFile:
    """One file an agent wants written into the workspace."""

    path: str
    content: str

    @property
    def is_test(self) -> bool:
        parts = PurePosixPath(self.path).parts
        name = PurePosixPath(self.path).name
        return (
            "tests" in parts
            or name.startswith("test_")
            or name.endswith("_test.py")
        )

    @property
    def is_python(self) -> bool:
        return self.path.endswith(".py")


@dataclass
class CodeSubmission:
    """A complete, self-contained set of files from a code-writing agent."""

    files: list[SourceFile]
    notes: str = ""
    #: Agent-reported claims are kept, but never treated as verification.
    #: Only the gate layer decides whether anything passed.
    self_assessment: str = ""
    raw: str = field(default="", repr=False)

    @property
    def source_files(self) -> list[SourceFile]:
        return [f for f in self.files if f.is_python and not f.is_test]

    @property
    def test_files(self) -> list[SourceFile]:
        return [f for f in self.files if f.is_python and f.is_test]

    @classmethod
    def parse(cls, text: str, *, require_tests: bool = True) -> "CodeSubmission":
        """Parse and validate raw model output.

        Raises :class:`ContractError` with a message intended to be fed back
        to the model on retry.
        """
        payload = extract_json(text)

        if "files" not in payload:
            raise ContractError(
                "the JSON object has no 'files' key",
                hint='Expected shape: {"files": [{"path": "app.py", '
                     '"content": "..."}], "notes": "..."}',
            )
        raw_files = payload["files"]
        if not isinstance(raw_files, list) or not raw_files:
            raise ContractError("'files' must be a non-empty array of objects")
        if len(raw_files) > MAX_FILES:
            raise ContractError(
                f"{len(raw_files)} files submitted but the limit is {MAX_FILES}"
            )

        files: list[SourceFile] = []
        seen: set[str] = set()
        for index, entry in enumerate(raw_files):
            if not isinstance(entry, dict):
                raise ContractError(f"files[{index}] is not an object")
            path = entry.get("path")
            content = entry.get("content")
            if not isinstance(path, str) or not path.strip():
                raise ContractError(f"files[{index}] has no usable 'path'")
            if not isinstance(content, str):
                raise ContractError(
                    f"files[{index}] ('{path}') has no 'content' string"
                )
            normalised = _validate_path(path, index)
            if normalised in seen:
                raise ContractError(f"'{normalised}' appears twice in 'files'")
            if len(content.encode("utf-8")) > MAX_FILE_BYTES:
                raise ContractError(f"'{normalised}' exceeds {MAX_FILE_BYTES} bytes")
            if not content.strip():
                raise ContractError(f"'{normalised}' has empty content")
            seen.add(normalised)
            files.append(SourceFile(path=normalised, content=content))

        submission = cls(
            files=files,
            notes=str(payload.get("notes") or ""),
            self_assessment=str(payload.get("self_assessment") or ""),
            raw=text,
        )
        submission.validate(require_tests=require_tests)
        return submission

    def validate(self, *, require_tests: bool = True) -> None:
        """Structural checks that need the whole submission in view."""
        if not self.source_files:
            raise ContractError(
                "no implementation file was submitted (only tests)",
                hint="Include the module that implements the requirement.",
            )
        if require_tests and not self.test_files:
            raise ContractError(
                "no test file was submitted",
                hint="Add a file under tests/ named test_*.py. Code without "
                     "tests cannot pass verification and will be rejected.",
            )

        # Syntax is checked here, not left to G1, because a truncated
        # response is the single most common failure mode and retrying the
        # agent immediately is far cheaper than a full gate run.
        for file in self.files:
            if not file.is_python:
                continue
            try:
                ast.parse(file.content)
            except SyntaxError as exc:
                raise ContractError(
                    f"'{file.path}' is not valid Python: {exc.msg} "
                    f"at line {exc.lineno}",
                    hint="This usually means the response was cut off. Return "
                         "the complete file.",
                ) from exc

    def to_dict(self) -> dict:
        return {
            "files": [{"path": f.path, "content": f.content} for f in self.files],
            "notes": self.notes,
            "self_assessment": self.self_assessment,
        }

    def summary(self) -> str:
        return (
            f"{len(self.source_files)} source file(s), "
            f"{len(self.test_files)} test file(s)"
        )


def _validate_path(path: str, index: int) -> str:
    """Reject anything that could write outside the workspace."""
    cleaned = path.strip().replace("\\", "/")
    if cleaned.startswith("/") or re.match(r"^[A-Za-z]:", cleaned):
        raise ContractError(
            f"files[{index}] path '{path}' is absolute; use a relative path"
        )
    pure = PurePosixPath(cleaned)
    if any(part == ".." for part in pure.parts):
        raise ContractError(
            f"files[{index}] path '{path}' escapes the workspace with '..'"
        )
    if pure.suffix not in ALLOWED_SUFFIXES:
        raise ContractError(
            f"files[{index}] path '{path}' has unsupported extension "
            f"'{pure.suffix}'; allowed: {', '.join(sorted(ALLOWED_SUFFIXES))}"
        )
    return pure.as_posix()


_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response.

    Models wrap JSON in code fences, prefix it with prose, or both. Rather
    than demanding perfect formatting, we extract tolerantly and fail with a
    precise message when there is genuinely nothing parseable.
    """
    if not text or not text.strip():
        raise ContractError("the response was empty")

    candidates: list[str] = []
    for match in _FENCE.findall(text):
        candidates.append(match.strip())
    candidates.append(text.strip())
    balanced = _balanced_object(text)
    if balanced:
        candidates.append(balanced)

    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload

    raise ContractError(
        "the response did not contain a parseable JSON object",
        hint="Return only a JSON object. Do not wrap it in explanation.",
    )


def _balanced_object(text: str) -> str | None:
    """Slice out the first brace-balanced object, ignoring braces in strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None
