"""Patchers: apply one diagnosed fix, to the one artefact it names.

Two agents, because the spec and the code are corrected under different
rules. Neither is told to "make the tests pass"; each is told what the
spec says and what to change.
"""

from __future__ import annotations

import json

from ..llm import TaskKind
from .base import Agent
from .contracts import Diagnosis, Spec, parse_patch, parse_spec_repair
from .diagnostician import render_files
from .contracts import CodeSubmission, SourceFile

_PATCH_SYSTEM = """\
You apply one targeted fix to files in a Python project, guided by a \
diagnosis and a specification.

Rules:
- Change only what the diagnosis asks for. Do not refactor, rename, reformat \
or "improve" anything else.
- The specification is the authority. When fixing a TEST, correct its \
expectation to what the specification states - never to whatever the \
implementation currently returns, and never by deleting it. When fixing \
CODE, make it follow the specification's rule text.
- Do NOT delete tests or weaken assertions to make a failure disappear. A \
test that is wrong is corrected; a test that is merely inconvenient stays.
- If the diagnosis is that the suite is too weak, ADD tests that pin the \
behaviour (exact values, both sides of each boundary, each stated error).
- Never hardcode credentials; never use eval/exec on input.
- Do not use symlinks, sockets or anything needing special privileges.
- Return the COMPLETE new content of every file you were given.

Reply with one JSON object and nothing else:

{
  "files": [{"path": "<exact path as given>", "content": "<complete file>"}],
  "notes": "<what you changed and why, in one or two sentences>"
}

Escape newlines as \\n. No markdown fences, no commentary outside the JSON.
"""

_PATCH_USER = """\
SPECIFICATION:
{spec}

DIAGNOSIS ({verdict}):
  root cause : {root_cause}
  evidence   : {evidence}
  change     : {fix_instruction}

FAILED CHECKS:
{failure}

PREVIOUS ATTEMPTS THAT DID NOT WORK (do not repeat them):
{history}

FILES TO FIX:
{files}
"""

_SPEC_SYSTEM = """\
You correct a flawed specification. A downstream check found that the \
specification contradicts itself - typically an example that disagrees with \
the rule text. Decide, from the rule text and ordinary meaning, which part \
is wrong, and fix only that.

Rules:
- Keep the module name and the exact set of functions and signatures. \
Renaming or adding functions is a requirements change a person must make.
- Prefer correcting an example over rewriting a rule, unless the rule itself \
is the part that is wrong.
- Every example must be correct under the final rules.

Reply with the COMPLETE corrected specification as a single JSON object in \
exactly the same shape as the specification you were given, and nothing else.
"""

_SPEC_USER = """\
CURRENT SPECIFICATION:
{spec}

DIAGNOSIS:
  root cause : {root_cause}
  evidence   : {evidence}
  change     : {fix_instruction}

FAILED CHECKS:
{failure}
"""


def _history_text(history: object) -> str:
    if not history:
        return "  (none)"
    assert isinstance(history, list)
    return "\n".join(
        f"  {i}. {h.get('verdict')}: {h.get('instruction')} -> {h.get('outcome')}"
        for i, h in enumerate(history, start=1)
    )


class PatcherAgent(Agent[CodeSubmission]):
    """Fixes implementation or test files, whichever the diagnosis names."""

    name = "patcher"
    task_kind = TaskKind.PATCH
    allowed_context = frozenset({
        "spec", "diagnosis", "failure", "files", "history", "max_test_removals",
    })

    def system_prompt(self) -> str:
        return _PATCH_SYSTEM

    def user_prompt(self, **context: object) -> str:
        spec = context["spec"]
        diagnosis = context["diagnosis"]
        files = context["files"]
        if not isinstance(spec, Spec) or not isinstance(diagnosis, Diagnosis):
            raise ValueError("PatcherAgent requires a Spec and a Diagnosis")
        assert isinstance(files, dict)
        return _PATCH_USER.format(
            spec=json.dumps(spec.to_dict(), indent=2),
            verdict=diagnosis.verdict,
            root_cause=diagnosis.root_cause,
            evidence=diagnosis.evidence,
            fix_instruction=diagnosis.fix_instruction,
            failure=str(context["failure"]),
            history=_history_text(context["history"]),
            files=render_files(files),
        )

    def parse(self, text: str, **context: object) -> CodeSubmission:
        files = context["files"]
        removals = context["max_test_removals"]
        assert isinstance(files, dict) and isinstance(removals, int)
        patched = parse_patch(
            text, allowed=set(files), before=files, max_test_removals=removals)
        return CodeSubmission(
            files=[SourceFile(path, content) for path, content in patched.items()],
            notes="",
        )


class SpecRepairAgent(Agent[Spec]):
    """Corrects a self-contradictory spec without changing its API."""

    name = "spec_repair"
    task_kind = TaskKind.PATCH
    allowed_context = frozenset({"spec", "diagnosis", "failure"})

    def system_prompt(self) -> str:
        return _SPEC_SYSTEM

    def user_prompt(self, **context: object) -> str:
        spec = context["spec"]
        diagnosis = context["diagnosis"]
        if not isinstance(spec, Spec) or not isinstance(diagnosis, Diagnosis):
            raise ValueError("SpecRepairAgent requires a Spec and a Diagnosis")
        return _SPEC_USER.format(
            spec=json.dumps(spec.to_dict(), indent=2),
            root_cause=diagnosis.root_cause,
            evidence=diagnosis.evidence,
            fix_instruction=diagnosis.fix_instruction,
            failure=str(context["failure"]),
        )

    def parse(self, text: str, **context: object) -> Spec:
        original = context["spec"]
        assert isinstance(original, Spec)
        return parse_spec_repair(text, original=original)
