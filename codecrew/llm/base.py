"""Provider-agnostic LLM contract.

Two constraints shape this layer:

* **No paid API is required.** Groq, Gemini and local Ollama all have free
  tiers or are free outright, and every provider here is interchangeable.
  Nothing in CodeCrew above this layer knows which one ran.
* **No third-party HTTP dependency.** Requests go out over ``urllib`` from
  the standard library, so the project installs with nothing but Python.
"""

from __future__ import annotations

import abc
import json
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field


@dataclass
class Message:
    """One turn of a chat exchange."""

    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    @property
    def total(self) -> int | None:
        if self.prompt_tokens is None and self.completion_tokens is None:
            return None
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)


@dataclass
class Completion:
    """A model response, plus everything needed to audit the call.

    Token counts and latency are recorded on every call because "cost per
    successful task" is one of the project's evaluation metrics, and it
    cannot be reconstructed after the fact.
    """

    text: str
    provider: str
    model: str
    latency_s: float
    usage: Usage = field(default_factory=Usage)
    #: Set when the provider stopped for a reason other than completion,
    #: e.g. hitting the token ceiling mid-answer.
    finish_reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class LLMError(RuntimeError):
    """Base class for provider failures."""


class ProviderUnavailable(LLMError):
    """The provider cannot be used at all, e.g. no API key configured."""


class RateLimited(LLMError):
    """Free tiers rate-limit aggressively; callers back off and retry."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class Provider(abc.ABC):
    """A chat-completion backend."""

    #: Short stable name used in configs and traces.
    name: str = "base"
    #: Model used when the caller does not name one.
    default_model: str = ""
    #: True when no credentials are needed (local models).
    free: bool = True

    @abc.abstractmethod
    def available(self) -> tuple[bool, str]:
        """Whether this provider can be called, and why not if it cannot."""

    @abc.abstractmethod
    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 120,
    ) -> Completion:
        """Send ``messages`` and return the response."""


def post_json(
    url: str,
    payload: dict,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 120,
) -> tuple[dict, float]:
    """POST JSON and return the decoded body with the elapsed time.

    Translates HTTP status codes into the exception types above so that
    callers can distinguish "retry in a moment" from "this will never work".
    """
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    # urllib's default "Python-urllib/3.x" agent is rejected by Cloudflare in
    # front of several providers (HTTP 403, error code 1010), which looks
    # exactly like a bad API key. Identify ourselves explicitly.
    request.add_header("User-Agent", "codecrew/0.1 (+https://github.com/ROOKIEEE12/Code_Crew_v2)")
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:600]
        if exc.code == 429:
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            raise RateLimited(
                f"rate limited (HTTP 429): {detail}",
                retry_after_s=float(retry_after) if retry_after else None,
            ) from exc
        if exc.code in (401, 403):
            hint = ""
            if "1010" in detail:
                hint = (
                    "\n(error 1010 is a Cloudflare client-signature block, "
                    "not a bad key: check the User-Agent and any proxy.)"
                )
            raise ProviderUnavailable(
                f"request refused (HTTP {exc.code}): {detail}{hint}"
            ) from exc
        if exc.code == 404:
            # Almost always a retired or misspelled model name. Retrying
            # cannot help, so this is raised as non-retryable with the fix.
            raise ProviderUnavailable(
                f"not found (HTTP 404): {detail}\n"
                "The model name is probably retired or misspelled. Free-tier "
                "models change often; set CODECREW_MODEL_STRONG and "
                "CODECREW_MODEL_FAST (or --strong-model / --fast-model)."
            ) from exc
        raise LLMError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ProviderUnavailable(f"network error: {exc.reason}") from exc

    elapsed = time.monotonic() - started
    try:
        return json.loads(raw), elapsed
    except json.JSONDecodeError as exc:
        raise LLMError(f"provider returned non-JSON body: {raw[:300]}") from exc
