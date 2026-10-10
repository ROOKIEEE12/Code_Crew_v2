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
import keyword
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
        submission = cls(
            files=parse_file_list(payload),
            notes=str(payload.get("notes") or ""),
            self_assessment=str(payload.get("self_assessment") or ""),
            raw=text,
        )
        submission.validate(require_tests=require_tests)
        return submission

    @classmethod
    def parse_implementation(cls, text: str, *, module: str) -> "CodeSubmission":
        """Output of an agent that writes the implementation only.

        Tests are rejected outright: in the multi-agent pipeline they are
        written by a different agent that never sees this code, and an
        implementer that also writes tests would quietly reintroduce the
        self-grading this architecture exists to remove.
        """
        payload = extract_json(text)
        submission = cls(
            files=parse_file_list(payload),
            notes=str(payload.get("notes") or ""),
            self_assessment=str(payload.get("self_assessment") or ""),
            raw=text,
        )
        tests = [f.path for f in submission.files if f.is_test]
        if tests:
            raise ContractError(
                f"test file(s) submitted: {', '.join(tests)}",
                hint="Do not write tests. A separate engineer writes them "
                     "independently from the same specification.",
            )
        if not any(f.path == f"{module}.py" for f in submission.files):
            raise ContractError(
                f"the module file '{module}.py' is missing",
                hint=f"The specification names the module '{module}', so the "
                     f"implementation must be in '{module}.py' at the root.",
            )
        submission._check_syntax()
        return submission

    @classmethod
    def parse_tests(cls, text: str, *, module: str) -> "CodeSubmission":
        """Output of an agent that writes tests only, from a spec."""
        payload = extract_json(text)
        submission = cls(
            files=parse_file_list(payload),
            notes=str(payload.get("notes") or ""),
            self_assessment=str(payload.get("self_assessment") or ""),
            raw=text,
        )
        stray = [f.path for f in submission.files if not f.is_test]
        if stray:
            raise ContractError(
                f"non-test file(s) submitted: {', '.join(stray)}",
                hint="Submit only test files under tests/ named test_*.py. "
                     "The implementation is written by someone else.",
            )
        for file in submission.files:
            if not file.is_python:
                continue
            if not re.search(rf"^\s*(from|import)\s+{re.escape(module)}\b",
                             file.content, re.MULTILINE):
                raise ContractError(
                    f"'{file.path}' never imports the module '{module}'",
                    hint=f"Use `from {module} import ...` so the tests "
                         f"exercise the specified module.",
                )
            if not re.search(r"^\s*(async\s+)?def\s+test_", file.content,
                             re.MULTILINE):
                raise ContractError(
                    f"'{file.path}' defines no test_* function"
                )
            if "assert" not in file.content and "raises" not in file.content:
                raise ContractError(
                    f"'{file.path}' contains no assertions",
                    hint="Tests that never assert cannot fail, so they "
                         "verify nothing.",
                )
        if not any(f.is_python for f in submission.files):
            raise ContractError("no Python test file was submitted")
        submission._check_syntax()
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

        self._check_syntax()

    def _check_syntax(self) -> None:
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


def parse_file_list(payload: dict) -> list[SourceFile]:
    """Validate the ``files`` array shared by every code-writing agent."""
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
    return files


# -- the specification ----------------------------------------------------------

MAX_SPEC_FUNCTIONS = 12
MAX_EXAMPLES = 20


def _is_identifier(name: object) -> bool:
    return isinstance(name, str) and name.isidentifier() and not keyword.iskeyword(name)


def _string_list(payload: dict, key: str, *, required: bool = False) -> list[str]:
    value = payload.get(key, [])
    if value is None:
        value = []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ContractError(f"'{key}' must be an array of strings")
    cleaned = [v.strip() for v in value if v.strip()]
    if required and not cleaned:
        raise ContractError(f"'{key}' must not be empty")
    return cleaned


@dataclass(frozen=True)
class Example:
    """One concrete input/output pair, as Python source expressions."""

    call: str
    result: str | None = None
    raises: str | None = None


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    signature: str
    description: str
    raises: list[str] = field(default_factory=list)
    examples: list[Example] = field(default_factory=list)


@dataclass
class Spec:
    """The contract between requirement and code.

    It is what both the Implementer and the Test-Author read, and nothing
    else of each other's. A wrong spec yields a perfectly tested wrong
    program, which is why a human approves it before any code exists.
    """

    summary: str
    module: str
    functions: list[FunctionSpec]
    constraints: list[str] = field(default_factory=list)
    security: list[str] = field(default_factory=list)
    #: Things the spec author had to guess. Shown at the approval
    #: checkpoint, because a silent guess is a silent bug.
    assumptions: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> "Spec":
        return cls.from_dict(extract_json(text))

    @classmethod
    def from_dict(cls, payload: dict) -> "Spec":
        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ContractError("'summary' must be a non-empty string")

        module = payload.get("module")
        if (
            not isinstance(module, str)
            or not _is_identifier(module)
            or not re.fullmatch(r"[a-z_][a-z0-9_]*", module)
        ):
            raise ContractError(
                f"'module' must be a lowercase snake_case Python identifier, "
                f"got {module!r}"
            )

        raw_functions = payload.get("functions")
        if not isinstance(raw_functions, list) or not raw_functions:
            raise ContractError("'functions' must be a non-empty array")
        if len(raw_functions) > MAX_SPEC_FUNCTIONS:
            raise ContractError(
                f"{len(raw_functions)} functions specified; limit is "
                f"{MAX_SPEC_FUNCTIONS}"
            )

        functions: list[FunctionSpec] = []
        names: set[str] = set()
        for index, raw in enumerate(raw_functions):
            if not isinstance(raw, dict):
                raise ContractError(f"functions[{index}] is not an object")
            functions.append(_parse_function(raw, index))
            name = functions[-1].name
            if name in names:
                raise ContractError(f"function '{name}' is specified twice")
            names.add(name)

        return cls(
            summary=summary.strip(),
            module=module,
            functions=functions,
            constraints=_string_list(payload, "constraints"),
            security=_string_list(payload, "security"),
            assumptions=_string_list(payload, "assumptions"),
            acceptance_criteria=_string_list(payload, "acceptance_criteria"),
        )

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "module": self.module,
            "functions": [
                {
                    "signature": f.signature,
                    "description": f.description,
                    "raises": list(f.raises),
                    "examples": [
                        {"call": e.call,
                         **({"result": e.result} if e.result is not None else {}),
                         **({"raises": e.raises} if e.raises is not None else {})}
                        for e in f.examples
                    ],
                }
                for f in self.functions
            ],
            "constraints": list(self.constraints),
            "security": list(self.security),
            "assumptions": list(self.assumptions),
            "acceptance_criteria": list(self.acceptance_criteria),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def render(self) -> str:
        """Human-readable form, for the approval checkpoint."""
        lines = [f"Module: {self.module}.py", f"Summary: {self.summary}", ""]
        for func in self.functions:
            lines.append(f"  {func.signature}")
            lines.append(f"      {func.description}")
            for exc in func.raises:
                lines.append(f"      raises: {exc}")
            for ex in func.examples:
                outcome = f"-> {ex.result}" if ex.raises is None else f"raises {ex.raises}"
                lines.append(f"      e.g. {ex.call} {outcome}")
            lines.append("")
        for title, items in (("Constraints", self.constraints),
                             ("Security", self.security),
                             ("Assumptions (CHECK THESE)", self.assumptions),
                             ("Acceptance criteria", self.acceptance_criteria)):
            if items:
                lines.append(f"{title}:")
                lines.extend(f"  - {item}" for item in items)
                lines.append("")
        return "\n".join(lines).rstrip()


def _parse_function(raw: dict, index: int) -> FunctionSpec:
    signature = raw.get("signature")
    if not isinstance(signature, str) or not signature.strip():
        raise ContractError(f"functions[{index}] has no 'signature'")
    signature = signature.strip()
    if signature.startswith("def "):
        signature = signature[4:].strip()
    try:
        tree = ast.parse(f"def {signature}:\n    pass\n")
        node = tree.body[0]
        if not isinstance(node, ast.FunctionDef) or len(tree.body) != 1:
            raise SyntaxError("not a single function signature")
    except SyntaxError as exc:
        raise ContractError(
            f"functions[{index}] signature {signature!r} is not a valid "
            f"Python signature",
            hint="Use the form `name(arg: type, ...) -> type`.",
        ) from exc

    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ContractError(f"function '{node.name}' has no 'description'")

    raw_examples = raw.get("examples") or []
    if not isinstance(raw_examples, list):
        raise ContractError(f"'{node.name}' examples must be an array")
    if len(raw_examples) > MAX_EXAMPLES:
        raise ContractError(f"'{node.name}' has more than {MAX_EXAMPLES} examples")

    examples = []
    for number, entry in enumerate(raw_examples):
        examples.append(_parse_example(entry, node.name, number))

    return FunctionSpec(
        name=node.name,
        signature=signature,
        description=description.strip(),
        raises=_string_list(raw, "raises"),
        examples=examples,
    )


def _parse_example(entry: object, function: str, number: int) -> Example:
    where = f"example {number} of '{function}'"
    if not isinstance(entry, dict):
        raise ContractError(f"{where} is not an object")
    call = entry.get("call")
    result = entry.get("result")
    raises = entry.get("raises")
    if not isinstance(call, str) or not call.strip():
        raise ContractError(f"{where} has no 'call'")
    if (result is None) == (raises is None):
        raise ContractError(
            f"{where} must have exactly one of 'result' or 'raises'",
            hint='Use {"call": "f(1)", "result": "2"} or '
                 '{"call": "f(-1)", "raises": "ValueError"}.',
        )
    for label, expr in (("call", call), ("result", result)):
        if expr is None:
            continue
        if not isinstance(expr, str):
            raise ContractError(
                f"{where}: '{label}' must be a Python expression written as "
                f"a string"
            )
        try:
            ast.parse(expr.strip(), mode="eval")
        except SyntaxError as exc:
            raise ContractError(
                f"{where}: '{label}' is not a valid Python expression: "
                f"{expr!r}"
            ) from exc
    if raises is not None and not _is_identifier(raises):
        raise ContractError(
            f"{where}: 'raises' must be an exception class name, got {raises!r}"
        )
    return Example(call=call.strip(), result=result.strip() if result else result,
                   raises=raises)


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
