"""Concrete providers, all free to run.

Priority order reflects the project's budget constraint, not quality:
Groq and Gemini have free tiers, Ollama is free locally, and Anthropic is
present but off by default because it needs paid credits.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .base import (
    Completion,
    LLMError,
    Message,
    Provider,
    ProviderUnavailable,
    Usage,
    post_json,
)


class GroqProvider(Provider):
    """Groq's OpenAI-compatible endpoint. Generous free tier, very fast."""

    name = "groq"
    default_model = "llama-3.3-70b-versatile"
    free = True
    endpoint = "https://api.groq.com/openai/v1/chat/completions"
    env_var = "GROQ_API_KEY"

    def available(self) -> tuple[bool, str]:
        if not os.environ.get(self.env_var):
            return False, f"{self.env_var} is not set"
        return True, ""

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 120,
    ) -> Completion:
        ok, reason = self.available()
        if not ok:
            raise ProviderUnavailable(reason)
        model = model or self.default_model
        data, elapsed = post_json(
            self.endpoint,
            {
                "model": model,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            headers={"Authorization": f"Bearer {os.environ[self.env_var]}"},
            timeout=timeout,
        )
        return _from_openai_shape(data, self.name, model, elapsed)


class GeminiProvider(Provider):
    """Google AI Studio. Free tier, separate request shape from OpenAI's."""

    name = "gemini"
    default_model = "gemini-2.0-flash"
    free = True
    env_var = "GEMINI_API_KEY"

    def available(self) -> tuple[bool, str]:
        if not os.environ.get(self.env_var):
            return False, f"{self.env_var} is not set"
        return True, ""

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 120,
    ) -> Completion:
        ok, reason = self.available()
        if not ok:
            raise ProviderUnavailable(reason)
        model = model or self.default_model

        # Gemini takes the system prompt in a dedicated field and uses
        # "model" where the others use "assistant".
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        contents = [
            {
                "role": "model" if m.role == "assistant" else "user",
                "parts": [{"text": m.content}],
            }
            for m in messages
            if m.role != "system"
        ]
        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={os.environ[self.env_var]}"
        )
        data, elapsed = post_json(url, payload, timeout=timeout)

        candidates = data.get("candidates") or []
        if not candidates:
            # Usually a safety block; surface it instead of returning "".
            raise LLMError(f"gemini returned no candidates: {json.dumps(data)[:400]}")
        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        meta = data.get("usageMetadata") or {}
        return Completion(
            text=text,
            provider=self.name,
            model=model,
            latency_s=elapsed,
            usage=Usage(
                prompt_tokens=meta.get("promptTokenCount"),
                completion_tokens=meta.get("candidatesTokenCount"),
            ),
            finish_reason=candidates[0].get("finishReason"),
        )


class OllamaProvider(Provider):
    """Locally hosted models. No key, no quota, no network egress."""

    name = "ollama"
    default_model = "qwen2.5-coder:7b"
    free = True
    endpoint = "http://localhost:11434/api/chat"

    def available(self) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3):
                return True, ""
        except (urllib.error.URLError, OSError):
            return False, "no Ollama server on localhost:11434"

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 300,
    ) -> Completion:
        model = model or self.default_model
        data, elapsed = post_json(
            self.endpoint,
            {
                "model": model,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens},
            },
            timeout=timeout,
        )
        return Completion(
            text=(data.get("message") or {}).get("content", ""),
            provider=self.name,
            model=model,
            latency_s=elapsed,
            usage=Usage(
                prompt_tokens=data.get("prompt_eval_count"),
                completion_tokens=data.get("eval_count"),
            ),
            finish_reason=data.get("done_reason"),
        )


class AnthropicProvider(Provider):
    """Present for completeness; needs paid credits, so it is never a default."""

    name = "anthropic"
    default_model = "claude-sonnet-5-5"
    free = False
    endpoint = "https://api.anthropic.com/v1/messages"
    env_var = "ANTHROPIC_API_KEY"

    def available(self) -> tuple[bool, str]:
        if not os.environ.get(self.env_var):
            return False, f"{self.env_var} is not set"
        return True, ""

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 120,
    ) -> Completion:
        ok, reason = self.available()
        if not ok:
            raise ProviderUnavailable(reason)
        model = model or self.default_model
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        payload: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": m.role, "content": m.content}
                for m in messages
                if m.role != "system"
            ],
        }
        if system:
            payload["system"] = system
        data, elapsed = post_json(
            self.endpoint,
            payload,
            headers={
                "x-api-key": os.environ[self.env_var],
                "anthropic-version": "2023-06-01",
            },
            timeout=timeout,
        )
        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        usage = data.get("usage") or {}
        return Completion(
            text=text,
            provider=self.name,
            model=model,
            latency_s=elapsed,
            usage=Usage(
                prompt_tokens=usage.get("input_tokens"),
                completion_tokens=usage.get("output_tokens"),
            ),
            finish_reason=data.get("stop_reason"),
        )


class ScriptedProvider(Provider):
    """Offline provider that replays canned responses.

    Not a toy: the pipeline must be testable and demonstrable with no key,
    no quota and no network, and the agent tests must be deterministic.
    Every response it returns is one a real model could have produced.
    """

    name = "scripted"
    default_model = "scripted-v1"
    free = True

    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[list[Message]] = []

    def available(self) -> tuple[bool, str]:
        return True, ""

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 120,
    ) -> Completion:
        self.calls.append(list(messages))
        if not self.responses:
            raise LLMError("scripted provider exhausted: no response left to return")
        return Completion(
            text=self.responses.pop(0),
            provider=self.name,
            model=model or self.default_model,
            latency_s=0.0,
            usage=Usage(prompt_tokens=0, completion_tokens=0),
        )


#: Registry in free-first order. ``auto`` selection walks this list.
PROVIDERS: dict[str, type[Provider]] = {
    "groq": GroqProvider,
    "gemini": GeminiProvider,
    "ollama": OllamaProvider,
    "anthropic": AnthropicProvider,
}

#: Tried in order by ``auto``. Anthropic is excluded deliberately: it costs
#: money, and a default that silently spends is the wrong default.
AUTO_ORDER: tuple[str, ...] = ("groq", "gemini", "ollama")


def _from_openai_shape(
    data: dict, provider: str, model: str, elapsed: float
) -> Completion:
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"{provider} returned no choices: {json.dumps(data)[:400]}")
    message = choices[0].get("message") or {}
    usage = data.get("usage") or {}
    return Completion(
        text=message.get("content") or "",
        provider=provider,
        model=model,
        latency_s=elapsed,
        usage=Usage(
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        ),
        finish_reason=choices[0].get("finish_reason"),
    )
