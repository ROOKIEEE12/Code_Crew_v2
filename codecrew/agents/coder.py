"""The Coder agent, and the single-agent baseline it provides.

Phase 1 deliberately builds the *baseline*: one agent that receives the
requirement and writes both the implementation and its tests, exactly as a
conventional AI coding assistant does. Phase 2 then splits it.

The baseline prompt is written to be strong, not a straw man. It asks for
edge cases, type hints and security hygiene explicitly. If the multi-agent
architecture is going to claim an improvement, it has to beat a competent
single agent, not a crippled one - otherwise the evaluation proves nothing.
"""

from __future__ import annotations

from ..llm import TaskKind
from .base import Agent
from .contracts import CodeSubmission

_SYSTEM = """\
You are an experienced Python engineer. You write a complete, self-contained \
implementation of a requirement, together with its test suite.

Requirements for your output:
- Target Python 3.10. Use type hints on all public functions.
- Put the implementation in a module at the workspace root (e.g. `app.py`).
- Put tests in `tests/test_<module>.py`, using pytest.
- Tests must cover boundary values and error paths, not only the happy path. \
Assert exact expected values rather than loose truthiness.
- Never hardcode credentials, API keys or passwords. Never use `eval`, `exec` \
or `shell=True` on caller-supplied input.
- Validate untrusted input explicitly.
- Add a short docstring to every public function.

Reply with a single JSON object and nothing else, in exactly this shape:

{
  "files": [
    {"path": "app.py", "content": "<complete file contents>"},
    {"path": "tests/test_app.py", "content": "<complete file contents>"}
  ],
  "notes": "<one or two sentences on your approach>",
  "self_assessment": "<what you are least confident about>"
}

Rules for the JSON:
- `content` must hold the entire file, not a fragment or a diff.
- Escape newlines as \\n inside the JSON strings.
- Do not wrap the JSON in markdown fences or add commentary around it.
"""

_USER = """\
Implement the following requirement.

REQUIREMENT:
{requirement}

The tests you write will be executed, and the implementation will then be \
checked independently by static analysis, branch-coverage measurement, \
security scanning and mutation testing. Mutation testing applies small \
changes to your implementation and checks whether your tests catch them, so \
assertions that merely confirm the code runs will be reported as a gap.
"""


class CoderAgent(Agent[CodeSubmission]):
    """Writes implementation and tests from a natural-language requirement."""

    name = "coder"
    task_kind = TaskKind.GENERATE
    allowed_context = frozenset({"requirement"})

    def system_prompt(self) -> str:
        return _SYSTEM

    def user_prompt(self, **context: object) -> str:
        requirement = context.get("requirement")
        if not isinstance(requirement, str) or not requirement.strip():
            raise ValueError("CoderAgent requires a non-empty 'requirement'")
        return _USER.format(requirement=requirement.strip())

    def parse(self, text: str, **context: object) -> CodeSubmission:
        return CodeSubmission.parse(text, require_tests=True)
