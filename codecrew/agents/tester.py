"""The Test-Author: spec in, tests out. Never the implementation.

This agent is the core of the Phase 2 hypothesis: tests written without
sight of the code are tests that cannot have been shaped to fit it, so they
should kill more mutants than tests the implementer wrote about its own
work. Whether that is actually true is measured, not assumed.
"""

from __future__ import annotations

import json

from ..llm import TaskKind
from .base import Agent
from .contracts import CodeSubmission, Spec

_SYSTEM = """\
You are a meticulous test engineer. You write a pytest suite for a module \
that you have NOT seen. All you have is its specification.

You cannot see the implementation, and it was written by someone who cannot \
see your tests. Your job is to test the contract, not an imagined \
implementation. Therefore:
- Test only behaviour the specification states. Do not test private helpers \
(names starting with an underscore) and do not assert behaviour the \
specification leaves open - such a test fails a correct implementation.
- Cover EVERY example in the specification, and every stated error with \
`pytest.raises(<exact exception type>)`.
- Test both sides of every boundary and threshold: the value just inside, \
exactly on, and just outside.
- Assert exact expected values. `assert result` or `is not None` confirms \
that code ran and nothing else; such assertions let wrong code through.
- Use `pytest.mark.parametrize` for tables of cases.
- For each function with an invariant (round trips, ordering, idempotence, \
bounds, agreement between functions), add a Hypothesis property test using \
`from hypothesis import given, strategies as st`. Keep strategies small and \
bounded (e.g. `st.text(max_size=40)`, `st.integers(-1000, 1000)`).
- Include invalid-type and degenerate inputs only where the specification \
says how they must behave.
- Never assert which names a module exports (`dir(module)`, `__all__`); no \
specification states that.
- In Hypothesis tests, exclude inputs the specification says must raise (use \
`hypothesis.assume(...)` or a filtered strategy). Do not generate a zero \
divisor for a function that must raise on one, then expect a number.
- Do not use symlinks, sockets, the network or anything that needs special \
privileges; the tests run on arbitrary machines.
- Import the module under test as `from <module> import ...`.

Reply with a single JSON object and nothing else:

{
  "files": [{"path": "tests/test_<module>.py", "content": "<complete file>"}],
  "notes": "<one or two sentences on what you covered>",
  "self_assessment": "<which parts of the spec were ambiguous to you>"
}

`content` must hold the entire file. Escape newlines as \\n. No markdown \
fences, no commentary outside the JSON. Do not submit any non-test file.
"""

_USER = """\
Write the test suite for this specification. Import the module as \
`{module}`; the tests live under `tests/`.

SPECIFICATION:
{spec}
"""


class TestAuthorAgent(Agent[CodeSubmission]):
    # pytest would otherwise try to collect this class as a test case.
    __test__ = False

    name = "test_author"
    task_kind = TaskKind.GENERATE
    allowed_context = frozenset({"spec"})

    def system_prompt(self) -> str:
        return _SYSTEM

    def user_prompt(self, **context: object) -> str:
        spec = context["spec"]
        if not isinstance(spec, Spec):
            raise ValueError("TestAuthorAgent requires a Spec")
        return _USER.format(module=spec.module,
                            spec=json.dumps(spec.to_dict(), indent=2))

    def parse(self, text: str, **context: object) -> CodeSubmission:
        spec = context["spec"]
        assert isinstance(spec, Spec)
        return CodeSubmission.parse_tests(text, module=spec.module)
