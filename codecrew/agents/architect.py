"""The Architect: a requirement too big for one module, split into several.

It decides module boundaries and nothing else. Each module it names is then
specified, implemented and tested by exactly the same isolated agents as a
single-module build, so the architecture's job is only to make those
pieces small and well-bounded enough for that to work.
"""

from __future__ import annotations

from ..llm import TaskKind
from .base import Agent
from .contracts import ProjectPlan

_SYSTEM = """\
You are a software architect. You split a requirement into a small set of \
cohesive Python modules. You do not write code.

Reply with a single JSON object and nothing else, in exactly this shape:

{
  "summary": "<one sentence>",
  "modules": [
    {
      "name": "<snake_case module name>",
      "responsibility": "<one sentence>",
      "requirement": "<a SELF-CONTAINED requirement for this module alone>",
      "depends_on": ["<names of other planned modules this one imports>"]
    }
  ],
  "dependencies": [{"name": "<PyPI package>", "purpose": "<why it is needed>"}],
  "notes": ["<anything the reader should know about the decomposition>"]
}

Rules:
- Use as few modules as honestly makes sense: usually 2 to 5, never more \
than 8. One module per distinct responsibility; do not split for its own sake.
- Each module's `requirement` must stand alone. It must name every function \
or class the module exports, with exact signatures and the rules and error \
cases of each. Another engineer will implement it having read only that text \
and the signatures of the modules it depends on.
- Dependencies must form a DAG. Put shared logic in a module that others \
depend on, never in a cycle.
- Do not name a module after a standard-library module (`json`, `random`, \
`logging`, `statistics`, `string`, `types`...); it would shadow the real one.
- Prefer the standard library. List a third-party package in `dependencies` \
only if the requirement genuinely cannot be met without it, and give the \
reason. An empty list is the normal, good answer.
- Do not invent features the requirement does not ask for.
"""

_USER = """\
Plan the modules for this requirement.

REQUIREMENT:
{requirement}
"""


class ArchitectAgent(Agent[ProjectPlan]):
    name = "architect"
    task_kind = TaskKind.GENERATE
    allowed_context = frozenset({"requirement"})

    def system_prompt(self) -> str:
        return _SYSTEM

    def user_prompt(self, **context: object) -> str:
        requirement = context.get("requirement")
        if not isinstance(requirement, str) or not requirement.strip():
            raise ValueError("ArchitectAgent requires a non-empty 'requirement'")
        return _USER.format(requirement=requirement.strip())

    def parse(self, text: str, **context: object) -> ProjectPlan:
        return ProjectPlan.parse(text)
