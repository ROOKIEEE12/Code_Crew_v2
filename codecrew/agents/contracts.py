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
            surface = re.search(
                rf"__all__|\bgetmembers\b|\b(?:dir|vars)\(\s*"
                rf"(?:{re.escape(module)}|module|mod)\s*\)", file.content)
            if surface:
                # Observed in the Phase 2 benchmark: three of six failed
                # workspaces asserted exactly which names a module exports.
                # No spec states that, so such a test fails a correct
                # implementation whenever it adds a helper.
                raise ContractError(
                    f"'{file.path}' asserts the module's public surface "
                    f"({surface.group(0).strip()!r})",
                    hint="Test behaviour the specification states. Which "
                         "names a module happens to export is not part of "
                         "any specification.",
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
    #: Signatures of already-built sibling modules this one may import, as
    #: ``module.func(args) -> type``. Set by the pipeline from the project
    #: plan, never by the model, so the interface cannot drift.
    uses: list[str] = field(default_factory=list)

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
        if module in STDLIB_NAMES:
            raise ContractError(
                f"module '{module}' would shadow the standard library module "
                f"of the same name",
                hint="Pick a more specific name; a file called "
                     f"{module}.py breaks every import of the real one.",
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
            uses=_string_list(payload, "uses"),
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
            "uses": list(self.uses),
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
        for title, items in (("Uses (already-built modules)", self.uses),
                             ("Constraints", self.constraints),
                             ("Security", self.security),
                             ("Assumptions (CHECK THESE)", self.assumptions),
                             ("Acceptance criteria", self.acceptance_criteria)):
            if items:
                lines.append(f"{title}:")
                lines.extend(f"  - {item}" for item in items)
                lines.append("")
        return "\n".join(lines).rstrip()


# -- the project plan (Phase 4) --------------------------------------------------

import sys as _sys

#: Names of standard-library modules. A project module with one of these
#: names silently replaces the real module for everything that imports it.
STDLIB_NAMES = frozenset(getattr(_sys, "stdlib_module_names", ()))

MAX_MODULES = 8
_DIST_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class PlanModule:
    name: str
    responsibility: str
    #: A self-contained requirement for this module, naming what it exports.
    requirement: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanDependency:
    """A third-party package the plan asks for, with a reason."""

    name: str
    purpose: str


@dataclass
class ProjectPlan:
    """The architect's decomposition of a requirement into modules.

    Each module is then specified, implemented and tested through the same
    pipeline as a single-module build. The plan is what a person approves
    first: it is cheaper to correct a module boundary here than after five
    files exist.
    """

    summary: str
    modules: list[PlanModule]
    dependencies: list[PlanDependency] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> "ProjectPlan":
        return cls.from_dict(extract_json(text))

    @classmethod
    def from_dict(cls, payload: dict) -> "ProjectPlan":
        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ContractError("'summary' must be a non-empty string")

        raw = payload.get("modules")
        if not isinstance(raw, list) or not raw:
            raise ContractError("'modules' must be a non-empty array")
        if len(raw) > MAX_MODULES:
            raise ContractError(
                f"{len(raw)} modules planned; limit is {MAX_MODULES}",
                hint="Merge modules that always change together.",
            )

        modules: list[PlanModule] = []
        names: set[str] = set()
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise ContractError(f"modules[{index}] is not an object")
            name = entry.get("name")
            if (not isinstance(name, str) or not _is_identifier(name)
                    or not re.fullmatch(r"[a-z_][a-z0-9_]*", name)):
                raise ContractError(
                    f"modules[{index}] name must be a lowercase snake_case "
                    f"identifier, got {name!r}")
            if name in STDLIB_NAMES:
                raise ContractError(
                    f"module '{name}' would shadow the standard library",
                    hint="Pick a more specific name.")
            if name in names:
                raise ContractError(f"module '{name}' is planned twice")
            names.add(name)
            for key in ("responsibility", "requirement"):
                value = entry.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise ContractError(
                        f"module '{name}' needs a non-empty '{key}'")
            depends = entry.get("depends_on") or []
            if not isinstance(depends, list) or not all(
                    isinstance(d, str) for d in depends):
                raise ContractError(
                    f"module '{name}' depends_on must be an array of names")
            modules.append(PlanModule(
                name=name,
                responsibility=entry["responsibility"].strip(),
                requirement=entry["requirement"].strip(),
                depends_on=tuple(depends),
            ))

        for module in modules:
            for dep in module.depends_on:
                if dep not in names:
                    raise ContractError(
                        f"module '{module.name}' depends on unknown module "
                        f"'{dep}'",
                        hint="Known modules: " + ", ".join(sorted(names)))
                if dep == module.name:
                    raise ContractError(
                        f"module '{module.name}' depends on itself")

        dependencies = []
        for index, entry in enumerate(payload.get("dependencies") or []):
            if not isinstance(entry, dict):
                raise ContractError(f"dependencies[{index}] is not an object")
            dep_name = entry.get("name")
            if not isinstance(dep_name, str) or not _DIST_NAME.fullmatch(dep_name):
                raise ContractError(
                    f"dependencies[{index}] has an invalid package name "
                    f"{dep_name!r}")
            purpose = entry.get("purpose")
            if not isinstance(purpose, str) or not purpose.strip():
                raise ContractError(
                    f"dependency '{dep_name}' must state its purpose",
                    hint="Prefer the standard library; every third-party "
                         "package needs a reason.")
            dependencies.append(PlanDependency(dep_name, purpose.strip()))

        plan = cls(summary=summary.strip(), modules=modules,
                   dependencies=dependencies,
                   notes=_string_list(payload, "notes"))
        plan.build_order()          # raises on a dependency cycle
        return plan

    def build_order(self) -> list[PlanModule]:
        """Dependencies first. Stable, so equal plans build identically."""
        by_name = {m.name: m for m in self.modules}
        remaining = {m.name: set(m.depends_on) for m in self.modules}
        ordered: list[PlanModule] = []
        while remaining:
            ready = [n for n in (m.name for m in self.modules)
                     if n in remaining and not remaining[n]]
            if not ready:
                raise ContractError(
                    "the modules have a circular dependency: "
                    + ", ".join(sorted(remaining)),
                    hint="Break the cycle by moving the shared part into a "
                         "module both depend on.")
            for name in ready:
                ordered.append(by_name[name])
                del remaining[name]
                for deps in remaining.values():
                    deps.discard(name)
        return ordered

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "modules": [
                {"name": m.name, "responsibility": m.responsibility,
                 "requirement": m.requirement, "depends_on": list(m.depends_on)}
                for m in self.modules
            ],
            "dependencies": [{"name": d.name, "purpose": d.purpose}
                             for d in self.dependencies],
            "notes": list(self.notes),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def render(self) -> str:
        lines = [f"Project: {self.summary}", ""]
        for module in self.build_order():
            deps = (f"  (uses: {', '.join(module.depends_on)})"
                    if module.depends_on else "")
            lines.append(f"  {module.name}.py{deps}")
            lines.append(f"      {module.responsibility}")
        lines.append("")
        if self.dependencies:
            lines.append("Third-party packages requested (CHECK THESE):")
            lines += [f"  - {d.name}: {d.purpose}" for d in self.dependencies]
        else:
            lines.append("Third-party packages: none (standard library only)")
        for note in self.notes:
            lines.append(f"Note: {note}")
        return "\n".join(lines).rstrip()


# -- reconciliation: diagnosis and patches ---------------------------------------

CODE_DEFECT = "code_defect"
TEST_DEFECT = "test_defect"
SPEC_DEFECT = "spec_defect"
VERDICTS = (CODE_DEFECT, TEST_DEFECT, SPEC_DEFECT)


@dataclass(frozen=True)
class Diagnosis:
    """Which artefact is wrong, decided against the spec.

    The three-way verdict is the point. A repair loop that only knows "make
    the tests pass" will happily bend correct code to satisfy a wrong test -
    the classic failure of automated debugging. Here the spec is the
    authority, and the Diagnostician must say whether the code, the tests or
    the spec itself contradicts it.
    """

    verdict: str
    root_cause: str
    fix_instruction: str
    #: The spec sentence or example this verdict rests on.
    evidence: str
    target_files: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str, *, files: dict[str, str]) -> "Diagnosis":
        payload = extract_json(text)
        verdict = payload.get("verdict")
        if verdict not in VERDICTS:
            raise ContractError(
                f"'verdict' must be one of {', '.join(VERDICTS)}, got {verdict!r}"
            )
        fields = {}
        for key in ("root_cause", "fix_instruction", "evidence"):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"'{key}' must be a non-empty string")
            fields[key] = value.strip()

        raw_targets = payload.get("target_files") or []
        if not isinstance(raw_targets, list) or not all(
                isinstance(t, str) for t in raw_targets):
            raise ContractError("'target_files' must be an array of paths")
        targets = tuple(t.strip().replace("\\", "/") for t in raw_targets)

        if verdict == SPEC_DEFECT:
            targets = ()
        else:
            if not targets:
                raise ContractError(
                    f"a {verdict} must name the file(s) to change in "
                    f"'target_files'"
                )
            unknown = [t for t in targets if t not in files]
            if unknown:
                raise ContractError(
                    f"unknown file(s) in 'target_files': {', '.join(unknown)}",
                    hint="Known files: " + ", ".join(sorted(files)),
                )
            wants_tests = verdict == TEST_DEFECT
            wrong = [t for t in targets
                     if SourceFile(t, "").is_test != wants_tests]
            if wrong:
                raise ContractError(
                    f"a {verdict} must target "
                    f"{'test' if wants_tests else 'implementation'} files, "
                    f"but got: {', '.join(wrong)}"
                )
        return cls(verdict=verdict, target_files=targets, **fields)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "root_cause": self.root_cause,
            "fix_instruction": self.fix_instruction,
            "evidence": self.evidence,
            "target_files": list(self.target_files),
        }


def count_test_functions(source: str) -> int:
    """Number of ``test*`` functions (including methods) in a module."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    return sum(
        1 for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    )


def parse_patch(
    text: str,
    *,
    allowed: set[str],
    before: dict[str, str],
    max_test_removals: int = 0,
) -> dict[str, str]:
    """Validate a patch: complete files, only the permitted ones.

    Two guards exist because a repair loop is under pressure to turn the
    verdict green by the cheapest route, and the cheapest route is deleting
    the evidence: it may not touch files outside its brief, and a test patch
    may not shrink the suite beyond the failures it was asked to fix.
    """
    payload = extract_json(text)
    files = parse_file_list(payload)
    paths = {f.path for f in files}

    stray = sorted(paths - allowed)
    if stray:
        raise ContractError(
            f"patch touches file(s) outside its brief: {', '.join(stray)}",
            hint="You may only modify: " + ", ".join(sorted(allowed)),
        )
    missing = sorted(allowed - paths)
    if missing:
        raise ContractError(
            f"patch omits file(s) it was asked to fix: {', '.join(missing)}",
            hint="Return the COMPLETE new content of every listed file.",
        )

    for file in files:
        if file.is_python:
            try:
                ast.parse(file.content)
            except SyntaxError as exc:
                raise ContractError(
                    f"'{file.path}' is not valid Python: {exc.msg} at line "
                    f"{exc.lineno}",
                    hint="The response may have been cut off; return the "
                         "whole file.",
                ) from exc

    test_paths = [f for f in files if f.is_test and f.is_python]
    if test_paths:
        old = sum(count_test_functions(before.get(f.path, "")) for f in test_paths)
        new = sum(count_test_functions(f.content) for f in test_paths)
        if new < 1:
            raise ContractError("the patch leaves no test functions")
        if old - new > max_test_removals:
            raise ContractError(
                f"the patch removes {old - new} test function(s) but only "
                f"{max_test_removals} failing test(s) were diagnosed",
                hint="Do not delete tests to make the suite pass. Correct "
                     "the wrong expectation instead, so the test still "
                     "checks the behaviour the specification states.",
            )
    return {f.path: f.content for f in files}


def parse_spec_repair(text: str, *, original: Spec) -> Spec:
    """A corrected spec may fix rules and examples, never rename the API."""
    fixed = Spec.parse(text)
    if fixed.module != original.module:
        raise ContractError(
            f"the module name changed from '{original.module}' to "
            f"'{fixed.module}'",
            hint="Keep the module name; renaming is a requirements change "
                 "that a person must decide.",
        )
    before = {f.name for f in original.functions}
    after = {f.name for f in fixed.functions}
    if before != after:
        raise ContractError(
            f"the set of functions changed: removed {sorted(before - after)}, "
            f"added {sorted(after - before)}",
            hint="Correct rules and examples only. Adding or removing "
                 "functions is a requirements change.",
        )
    return fixed


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
