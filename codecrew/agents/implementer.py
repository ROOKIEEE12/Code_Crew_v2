"""The Implementer: spec in, implementation out. Never tests, never the
original requirement text.

Seeing only the approved spec matters for the same reason the Test-Author
does: both must derive their work from one shared, human-reviewed contract,
so that a disagreement between them is a *signal* about the spec rather
than two models quietly agreeing with each other.
"""

from __future__ import annotations

import json

from ..llm import TaskKind
from .base import Agent
from .contracts import CodeSubmission, Spec

_SYSTEM = """\
You are an experienced Python engineer. You implement a module exactly as \
described by a specification.

A different engineer, who cannot see your code, is writing the tests from \
the same specification. They will test every example, every stated error and \
every boundary. Therefore:
- Implement the specification exactly: the stated names, signatures, return \
types and exception types. Behaviour not in the specification is not wanted.
- Raise precisely the exceptions the specification names, for precisely the \
stated conditions.
- Do NOT write tests. Do not include any test file.

Quality requirements:
- Target Python 3.10 with type hints on public functions and a short \
docstring on each.
- Never hardcode credentials. Never use `eval`, `exec` or `shell=True` on \
caller-supplied input. Validate untrusted input explicitly.
- Standard library only unless the specification says otherwise.

Reply with a single JSON object and nothing else:

{
  "files": [{"path": "<module>.py", "content": "<complete file contents>"}],
  "notes": "<one or two sentences on your approach>",
  "self_assessment": "<what you are least confident about>"
}

`content` must hold the entire file. Escape newlines as \\n. No markdown \
fences, no commentary outside the JSON.
"""

_USER = """\
Implement this specification. The module file must be `{module}.py` at the \
workspace root.

SPECIFICATION:
{spec}
"""


class ImplementerAgent(Agent[CodeSubmission]):
    name = "implementer"
    task_kind = TaskKind.GENERATE
    allowed_context = frozenset({"spec"})

    def system_prompt(self) -> str:
        return _SYSTEM

    def user_prompt(self, **context: object) -> str:
        spec = context["spec"]
        if not isinstance(spec, Spec):
            raise ValueError("ImplementerAgent requires a Spec")
        return _USER.format(module=spec.module,
                            spec=json.dumps(spec.to_dict(), indent=2))

    def parse(self, text: str, **context: object) -> CodeSubmission:
        spec = context["spec"]
        assert isinstance(spec, Spec)
        return CodeSubmission.parse_implementation(text, module=spec.module)
