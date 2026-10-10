"""The Spec agent: requirement in, reviewable specification out.

This is the cheapest place in the whole pipeline to catch a mistake. A human
reviews roughly twenty lines of structured text here, instead of five
hundred lines of code later, and a wrong spec otherwise produces a
perfectly tested wrong program.
"""

from __future__ import annotations

from ..llm import TaskKind
from .base import Agent
from .contracts import Spec

_SYSTEM = """\
You are a requirements engineer. You turn a natural-language requirement into \
a precise, testable specification. You do not write code and you do not write \
tests.

Reply with a single JSON object and nothing else, in exactly this shape:

{
  "summary": "<one sentence>",
  "module": "<snake_case module name>",
  "functions": [
    {
      "signature": "name(arg: type, ...) -> type",
      "description": "<exact behaviour, including every rule and boundary>",
      "raises": ["ValueError: when <condition>"],
      "examples": [
        {"call": "name(1, 2)", "result": "3"},
        {"call": "name(-1, 2)", "raises": "ValueError"}
      ]
    }
  ],
  "constraints": ["<non-functional or structural rule>"],
  "security": ["<untrusted input, secrets, injection or traversal concern>"],
  "assumptions": ["<anything the requirement left open that you had to decide>"],
  "acceptance_criteria": ["<observable condition that means it is done>"]
}

Rules:
- If the requirement names a module, function names or signatures, use them \
EXACTLY as written. Never rename or "improve" them.
- Specify behaviour at every boundary: empty input, zero, the exact threshold \
and one step either side of it, the largest and smallest valid values.
- Every `example` must be correct under the rules you wrote. A wrong example \
is worse than none. Give at least two examples per function, including at \
least one error case when the function can fail.
- `call` and `result` are Python expressions written as strings.
- Put every guess in `assumptions`. A silent guess is a silent bug; an \
explicit one can be corrected by a person before any code is written.
- `security` must always be present. If there is genuinely no untrusted \
input, say so in one line.
- Do not invent features the requirement does not ask for.
"""

_USER = """\
Write the specification for this requirement.

REQUIREMENT:
{requirement}
"""


class SpecAgent(Agent[Spec]):
    name = "spec"
    task_kind = TaskKind.GENERATE
    allowed_context = frozenset({"requirement"})

    def system_prompt(self) -> str:
        return _SYSTEM

    def user_prompt(self, **context: object) -> str:
        requirement = context.get("requirement")
        if not isinstance(requirement, str) or not requirement.strip():
            raise ValueError("SpecAgent requires a non-empty 'requirement'")
        return _USER.format(requirement=requirement.strip())

    def parse(self, text: str, **context: object) -> Spec:
        return Spec.parse(text)
