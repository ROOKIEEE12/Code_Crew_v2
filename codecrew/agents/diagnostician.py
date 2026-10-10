"""The Diagnostician: a failing verdict in, "which artefact is wrong" out.

Every other agent in the system is deliberately blind to something. This
one is not: it sees the spec, the code, the tests and the failure evidence,
because arbitrating between them requires all four. What keeps that safe is
that it holds no authority of its own. It may only decide which artefact
contradicts the spec, and its verdict is then a typed contract that a
patcher must follow.
"""

from __future__ import annotations

import json

from ..llm import TaskKind
from .base import Agent
from .contracts import Diagnosis, Spec

_SYSTEM = """\
You are the arbiter in a software verification pipeline. A specification, an \
implementation and a test suite were produced independently, and the \
pipeline's checks have failed. Decide which artefact is wrong.

THE SPECIFICATION IS THE AUTHORITY. Its rule text (the summary and each \
function's `description` and `raises`) defines correct behaviour. Its \
`examples` are illustrations that should agree with the rule text.

You must return exactly one verdict:

code_defect - The implementation violates the spec's rule text, or fails a \
spec example that is consistent with that text. Fix the implementation.

test_defect - The implementation is right but a test is wrong or too weak. \
This includes: a test asserting something the spec never states (the \
module's exported names, a TypeError the spec does not mention, behaviour \
the spec leaves open); a test whose expected value contradicts the spec; a \
property test with its own bug (for example generating an input the spec \
says must raise); a test that depends on the machine (symlinks, network, \
privileges); or a suite too weak to detect injected bugs (surviving \
mutants, low coverage). Fix the tests.

spec_defect - The spec contradicts itself: an example disagrees with the \
spec's own rule text, or two rules conflict. Fix the spec.

How to decide:
1. Read the rule text first, then check each failing example/test against \
it, not against the implementation's current output.
2. NEVER choose code_defect merely because a test fails. First confirm the \
test's expectation actually follows from the rule text.
3. If a spec example disagrees with the rule text, that is spec_defect, even \
though the implementation is "wrong" against the example.
4. Fix the single root cause that explains the most failures.

Reply with one JSON object and nothing else:

{
  "verdict": "code_defect | test_defect | spec_defect",
  "target_files": ["<path of each file to change; [] for spec_defect>"],
  "root_cause": "<one or two sentences>",
  "evidence": "<quote the spec sentence or example this rests on>",
  "fix_instruction": "<precise, minimal change to make>"
}

`target_files` must be paths from the file list. For code_defect they are \
implementation files, for test_defect they are test files.
"""

_USER = """\
SPECIFICATION:
{spec}

FAILED CHECKS:
{failure}

FILES:
{files}
"""

_MAX_FILE_CHARS = 12_000


def render_files(files: dict[str, str]) -> str:
    blocks = []
    for path in sorted(files):
        content = files[path]
        if len(content) > _MAX_FILE_CHARS:
            content = content[:_MAX_FILE_CHARS] + "\n# ...(truncated)..."
        blocks.append(f"--- {path} ---\n{content}")
    return "\n\n".join(blocks)


class DiagnosticianAgent(Agent[Diagnosis]):
    name = "diagnostician"
    task_kind = TaskKind.DIAGNOSE
    allowed_context = frozenset({"spec", "failure", "files"})

    def system_prompt(self) -> str:
        return _SYSTEM

    def user_prompt(self, **context: object) -> str:
        spec = context["spec"]
        if not isinstance(spec, Spec):
            raise ValueError("DiagnosticianAgent requires a Spec")
        files = context["files"]
        assert isinstance(files, dict)
        return _USER.format(
            spec=json.dumps(spec.to_dict(), indent=2),
            failure=str(context["failure"]),
            files=render_files(files),
        )

    def parse(self, text: str, **context: object) -> Diagnosis:
        files = context["files"]
        assert isinstance(files, dict)
        return Diagnosis.parse(text, files=files)
