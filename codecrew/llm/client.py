"""The call layer: provider selection, task routing, retries and tracing."""

from __future__ import annotations

import enum
import json
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path

from ..trace import NullTracer, Tracer
from .base import Completion, LLMError, Message, Provider, ProviderUnavailable, RateLimited
from .providers import AUTO_ORDER, PROVIDERS, ScriptedProvider

#: Ceiling for the token budget when an answer comes back empty and truncated.
MAX_TOKEN_BUDGET = 16384


class TaskKind(str, enum.Enum):
    """What an agent is asking the model to do.

    Routing exists because the hard reasoning (diagnosing a stack trace,
    writing a minimal patch) deserves the strongest model available, while
    formatting a docstring does not. Under a zero-budget constraint this is
    the difference between staying inside a free tier and not.
    """

    #: Mechanical restructuring, docstrings, summaries.
    FORMAT = "format"
    #: Writing new code or tests from a specification.
    GENERATE = "generate"
    #: Root-causing a gate failure. Hardest reasoning in the system.
    DIAGNOSE = "diagnose"
    #: Producing a minimal fix for a diagnosed failure.
    PATCH = "patch"


#: Which tier each task kind draws from.
TASK_TIERS: dict[TaskKind, str] = {
    TaskKind.FORMAT: "fast",
    TaskKind.GENERATE: "strong",
    TaskKind.DIAGNOSE: "strong",
    TaskKind.PATCH: "strong",
}


@dataclass
class ModelTiers:
    """Model names per tier, per provider.

    ``None`` means "use the provider's default", which keeps the config
    short while still allowing a deliberate split.
    """

    fast: str | None = None
    strong: str | None = None

    def model_for(self, kind: TaskKind) -> str | None:
        return self.fast if TASK_TIERS[kind] == "fast" else self.strong


#: Free-tier defaults per provider, last checked 2026-10-10.
#:
#: Hosted free-tier model names churn quickly: Groq retired
#: llama-3.3-70b-versatile on 2026-08-16 and Google deprecated Gemini 2.0
#: Flash in mid-2026, so the previous defaults would have failed on the
#: first call. Treat these as best-effort and override without a code change
#: via CODECREW_MODEL_FAST / CODECREW_MODEL_STRONG or --fast-model /
#: --strong-model.
DEFAULT_TIERS: dict[str, ModelTiers] = {
    "groq": ModelTiers(fast="openai/gpt-oss-120b", strong="openai/gpt-oss-120b"),
    "gemini": ModelTiers(fast="gemini-2.5-flash-lite", strong="gemini-2.5-flash"),
    "ollama": ModelTiers(fast="qwen2.5-coder:7b", strong="qwen2.5-coder:7b"),
    "anthropic": ModelTiers(fast="claude-haiku-5-5", strong="claude-sonnet-5-5"),
}

ENV_FAST = "CODECREW_MODEL_FAST"
ENV_STRONG = "CODECREW_MODEL_STRONG"


def _apply_env_overrides(tiers: ModelTiers) -> ModelTiers:
    """Environment beats defaults, so a retired model is a one-line fix."""
    return ModelTiers(
        fast=os.environ.get(ENV_FAST) or tiers.fast,
        strong=os.environ.get(ENV_STRONG) or tiers.strong,
    )


@dataclass
class LLMConfig:
    provider: str = "auto"
    #: Path to a JSON array of canned responses, used with the "scripted"
    #: provider. Makes a run reproducible with no key and no network, which
    #: is what the offline demo and the evaluation replay depend on.
    script: str | None = None
    tiers: ModelTiers | None = None
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout_s: int = 180
    #: Free tiers rate-limit hard, so retrying is the normal path, not an
    #: error path. Backoff is exponential with jitter.
    max_retries: int = 4
    backoff_base_s: float = 2.0


class LLMClient:
    """Single entry point for every model call in CodeCrew."""

    def __init__(
        self,
        config: LLMConfig | None = None,
        provider: Provider | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.config = config or LLMConfig()
        self.tracer = tracer or NullTracer()
        self.provider = provider or select_provider(
            self.config.provider, self.config.script
        )
        base_tiers = (
            self.config.tiers
            or DEFAULT_TIERS.get(self.provider.name)
            or ModelTiers()
        )
        self.tiers = _apply_env_overrides(base_tiers)

    def complete(
        self,
        messages: list[Message],
        *,
        kind: TaskKind = TaskKind.GENERATE,
        temperature: float | None = None,
        max_tokens: int | None = None,
        agent: str | None = None,
    ) -> Completion:
        """Call the model, retrying transient rate limits."""
        model = self.tiers.model_for(kind)
        temp = self.config.temperature if temperature is None else temperature
        limit = max_tokens or self.config.max_tokens
        last_error: Exception | None = None

        for attempt in range(1, self.config.max_retries + 1):
            try:
                completion = self.provider.complete(
                    messages,
                    model=model,
                    temperature=temp,
                    max_tokens=limit,
                    timeout=self.config.timeout_s,
                )
            except RateLimited as exc:
                last_error = exc
                delay = exc.retry_after_s or self._backoff(attempt)
                self.tracer.event(
                    "llm_retry", agent=agent, task=kind.value, attempt=attempt,
                    reason="rate_limited", sleep_s=round(delay, 2),
                )
                if attempt == self.config.max_retries:
                    break
                time.sleep(delay)
                continue
            except ProviderUnavailable:
                raise
            except LLMError as exc:
                last_error = exc
                self.tracer.event(
                    "llm_retry", agent=agent, task=kind.value, attempt=attempt,
                    reason=type(exc).__name__, error=str(exc)[:300],
                )
                if attempt == self.config.max_retries:
                    break
                time.sleep(self._backoff(attempt))
                continue

            self.tracer.event(
                "llm_call",
                agent=agent,
                task=kind.value,
                provider=completion.provider,
                model=completion.model,
                temperature=temp,
                latency_s=round(completion.latency_s, 3),
                prompt_tokens=completion.usage.prompt_tokens,
                completion_tokens=completion.usage.completion_tokens,
                finish_reason=completion.finish_reason,
                chars=len(completion.text),
            )
            if completion.finish_reason in ("length", "max_tokens", "MAX_TOKENS"):
                # A truncated answer is a silent corruption source: the code
                # it produces will be syntactically incomplete.
                self.tracer.event("llm_truncated", agent=agent, model=completion.model)
                if not completion.text.strip() and limit < MAX_TOKEN_BUDGET \
                        and attempt < self.config.max_retries:
                    # Found live with gpt-oss-20b: a reasoning model spent the
                    # whole budget thinking and returned nothing. That is not
                    # an answer to judge, so ask again with room to answer.
                    limit = min(limit * 2, MAX_TOKEN_BUDGET)
                    last_error = LLMError("empty answer: the token budget went on reasoning")
                    self.tracer.event(
                        "llm_retry", agent=agent, task=kind.value, attempt=attempt,
                        reason="empty_truncated", max_tokens=limit,
                    )
                    continue
            return completion

        raise LLMError(
            f"{self.provider.name} failed after {self.config.max_retries} attempts: "
            f"{last_error}"
        )

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with jitter, to avoid synchronised retries."""
        return self.config.backoff_base_s * (2 ** (attempt - 1)) * (
            1 + random.random() * 0.25
        )

    def describe(self) -> str:
        return (
            f"{self.provider.name} (fast={self.tiers.fast or 'default'}, "
            f"strong={self.tiers.strong or 'default'})"
        )


def select_provider(name: str = "auto", script: str | None = None) -> Provider:
    """Resolve a provider name, preferring free options.

    ``auto`` walks the free-first order and picks the first usable one, so a
    user with only a Gemini key, or only Ollama running, needs no config.
    """
    if name == "scripted":
        return ScriptedProvider(_load_script(script))
    if name != "auto":
        if name not in PROVIDERS:
            raise ProviderUnavailable(
                f"unknown provider {name!r}; known: {', '.join(PROVIDERS)}"
            )
        provider = PROVIDERS[name]()
        ok, reason = provider.available()
        if not ok:
            raise ProviderUnavailable(f"{name} is not usable: {reason}")
        return provider

    reasons = []
    for candidate in AUTO_ORDER:
        provider = PROVIDERS[candidate]()
        ok, reason = provider.available()
        if ok:
            return provider
        reasons.append(f"{candidate}: {reason}")
    raise ProviderUnavailable(
        "no free LLM provider is configured.\n  "
        + "\n  ".join(reasons)
        + "\n\nSet one of GROQ_API_KEY or GEMINI_API_KEY (both have free "
          "tiers), or run Ollama locally. Use --provider scripted to "
          "exercise the pipeline with no model at all."
    )


def _load_script(script: str | None) -> list[str]:
    """Load canned responses for the scripted provider.

    Accepts a JSON array of strings, or an array of objects with a "text"
    field, so a recorded trace can be replayed directly.
    """
    if not script:
        return []
    path = Path(script)
    if not path.exists():
        raise ProviderUnavailable(f"script file not found: {script}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProviderUnavailable(f"script file is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ProviderUnavailable("script file must contain a JSON array")

    responses: list[str] = []
    for index, entry in enumerate(data):
        if isinstance(entry, str):
            responses.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("text"), str):
            responses.append(entry["text"])
        else:
            raise ProviderUnavailable(
                f"script entry {index} is neither a string nor an object "
                f"with a 'text' field"
            )
    return responses


def provider_status() -> list[tuple[str, bool, str]]:
    """Availability of every provider, for the CLI's doctor command."""
    status = []
    for name, cls in PROVIDERS.items():
        ok, reason = cls().available()
        status.append((name, ok, reason))
    return status
