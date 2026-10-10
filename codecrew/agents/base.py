"""Agent contract.

Every agent in CodeCrew obeys the same three rules:

1. Its output is parsed into a typed contract before anything downstream
   sees it. A contract violation is retried with the specific error, not
   patched over.
2. It receives only the context it needs. Agents do not share a blackboard;
   Phase 2 depends on this, because the Test-Author must never see the
   implementation.
3. It may not assert that anything passed. Only the gate layer decides.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from ..llm import LLMClient, LLMError, Message, TaskKind
from ..trace import NullTracer, Tracer
from .contracts import ContractError

T = TypeVar("T")


@dataclass
class AgentResult(Generic[T]):
    """What an agent produced, and what it cost to get there."""

    output: T
    agent: str
    attempts: int
    #: Contract errors encountered and recovered from, kept for the report
    #: and for the per-agent accuracy evaluation.
    rejections: list[str] = field(default_factory=list)
    raw: str = field(default="", repr=False)


class AgentFailure(RuntimeError):
    """The agent could not produce contract-valid output within its budget."""

    def __init__(self, agent: str, attempts: int, rejections: list[str]) -> None:
        super().__init__(
            f"{agent} failed to produce valid output in {attempts} attempt(s): "
            + "; ".join(rejections[-2:])
        )
        self.agent = agent
        self.attempts = attempts
        self.rejections = rejections


class Agent(abc.ABC, Generic[T]):
    """One specialised worker with a typed output contract."""

    #: Stable name used in traces and the activity log.
    name: str = "agent"
    #: Routing hint: what kind of thinking this agent does.
    task_kind: TaskKind = TaskKind.GENERATE
    #: Contract-repair attempts before giving up.
    max_attempts: int = 3

    def __init__(self, client: LLMClient, tracer: Tracer | None = None) -> None:
        self.client = client
        self.tracer = tracer or getattr(client, "tracer", None) or NullTracer()

    @abc.abstractmethod
    def system_prompt(self) -> str:
        """The agent's role and output format. Must specify the contract."""

    @abc.abstractmethod
    def user_prompt(self, **context: object) -> str:
        """The task, built from only the context this agent is allowed."""

    @abc.abstractmethod
    def parse(self, text: str) -> T:
        """Validate raw output into the typed contract, or raise ContractError."""

    def run(self, **context: object) -> AgentResult[T]:
        """Call the model and repair contract violations in place.

        A rejected response is retried with the validation error appended, so
        the model is told precisely what was wrong rather than being asked
        again and hoped at.
        """
        messages = [
            Message("system", self.system_prompt()),
            Message("user", self.user_prompt(**context)),
        ]
        rejections: list[str] = []

        for attempt in range(1, self.max_attempts + 1):
            self.tracer.event("agent_step", agent=self.name, attempt=attempt)
            try:
                completion = self.client.complete(
                    messages, kind=self.task_kind, agent=self.name
                )
            except LLMError as exc:
                self.tracer.event("agent_error", agent=self.name, attempt=attempt,
                                  error=str(exc)[:300])
                raise

            try:
                output = self.parse(completion.text)
            except ContractError as exc:
                rejections.append(exc.message)
                self.tracer.event(
                    "contract_rejected", agent=self.name, attempt=attempt,
                    reason=exc.message,
                )
                messages.append(Message("assistant", completion.text))
                messages.append(Message("user", exc.feedback()))
                continue

            self.tracer.event(
                "agent_done", agent=self.name, attempt=attempt,
                rejections=len(rejections),
            )
            return AgentResult(
                output=output,
                agent=self.name,
                attempts=attempt,
                rejections=rejections,
                raw=completion.text,
            )

        self.tracer.event("agent_failed", agent=self.name,
                          attempts=self.max_attempts, rejections=rejections)
        raise AgentFailure(self.name, self.max_attempts, rejections)
