"""Request-shape tests for the live providers.

No network and no key: ``post_json`` is replaced with a recorder, so these
assert exactly what would have been sent and how the reply is read. The
providers could not be exercised against real endpoints without a key, so
this is the strongest offline guarantee available - it catches the
mistakes that are mine (wrong field name, missing header, wrong role).
"""

from __future__ import annotations

import pytest

from codecrew.llm import (
    AnthropicProvider,
    GeminiProvider,
    GroqProvider,
    LLMClient,
    LLMConfig,
    Message,
    OllamaProvider,
    ProviderUnavailable,
)
from codecrew.llm.base import post_json

MESSAGES = [
    Message("system", "You are terse."),
    Message("user", "hello"),
    Message("assistant", "hi"),
    Message("user", "again"),
]


class Recorder:
    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.url = ""
        self.payload: dict = {}
        self.headers: dict = {}

    def __call__(self, url, payload, *, headers=None, timeout=120):
        self.url, self.payload, self.headers = url, payload, headers or {}
        return self.reply, 0.25


@pytest.fixture
def record(monkeypatch):
    def install(reply: dict) -> Recorder:
        recorder = Recorder(reply)
        monkeypatch.setattr("codecrew.llm.providers.post_json", recorder)
        return recorder
    return install


def test_groq_sends_openai_shape_with_bearer_auth(record, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    rec = record({"choices": [{"message": {"content": "ok"},
                               "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 4, "completion_tokens": 1}})
    out = GroqProvider().complete(MESSAGES, temperature=0.0, max_tokens=99)

    assert rec.url.endswith("/openai/v1/chat/completions")
    assert rec.headers["Authorization"] == "Bearer k"
    assert rec.payload["model"] == GroqProvider.default_model
    assert rec.payload["max_tokens"] == 99
    assert [m["role"] for m in rec.payload["messages"]] == [
        "system", "user", "assistant", "user"]
    assert out.text == "ok" and out.usage.total == 5


def test_gemini_moves_system_prompt_and_renames_assistant(record, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    rec = record({"candidates": [{"content": {"parts": [{"text": "he"},
                                                         {"text": "llo"}]},
                                  "finishReason": "STOP"}],
                  "usageMetadata": {"promptTokenCount": 7,
                                    "candidatesTokenCount": 2}})
    out = GeminiProvider().complete(MESSAGES, max_tokens=50)

    assert rec.payload["systemInstruction"]["parts"][0]["text"] == "You are terse."
    roles = [c["role"] for c in rec.payload["contents"]]
    assert roles == ["user", "model", "user"], "assistant must become 'model'"
    assert rec.payload["generationConfig"]["maxOutputTokens"] == 50
    assert "key=k" in rec.url
    assert out.text == "hello"            # multi-part replies are joined
    assert out.usage.total == 9


def test_gemini_with_no_candidates_is_an_error_not_empty_text(record, monkeypatch):
    from codecrew.llm import LLMError

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    record({"promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(LLMError, match="no candidates"):
        GeminiProvider().complete(MESSAGES)


def test_anthropic_uses_top_level_system_and_version_header(record, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    rec = record({"content": [{"type": "text", "text": "yo"}],
                  "usage": {"input_tokens": 3, "output_tokens": 1},
                  "stop_reason": "end_turn"})
    out = AnthropicProvider().complete(MESSAGES)

    assert rec.payload["system"] == "You are terse."
    assert all(m["role"] != "system" for m in rec.payload["messages"])
    assert rec.headers["x-api-key"] == "k"
    assert rec.headers["anthropic-version"]
    assert out.text == "yo" and out.finish_reason == "end_turn"


def test_ollama_disables_streaming_and_maps_token_options(record):
    rec = record({"message": {"content": "local"}, "prompt_eval_count": 5,
                  "eval_count": 2, "done_reason": "stop"})
    out = OllamaProvider().complete(MESSAGES, max_tokens=33, temperature=0.3)

    assert rec.payload["stream"] is False
    assert rec.payload["options"] == {"temperature": 0.3, "num_predict": 33}
    assert out.text == "local" and out.usage.total == 7


@pytest.mark.parametrize("cls", [GroqProvider, GeminiProvider, AnthropicProvider])
def test_missing_key_is_reported_before_any_request(cls, monkeypatch):
    for var in ("GROQ_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ProviderUnavailable):
        cls().complete(MESSAGES)


# -- model-name handling (free-tier names churn) -------------------------------

def test_env_overrides_beat_default_models(monkeypatch):
    monkeypatch.setenv("CODECREW_MODEL_STRONG", "my/strong")
    monkeypatch.setenv("CODECREW_MODEL_FAST", "my/fast")
    client = LLMClient(LLMConfig(provider="scripted"),
                       provider=GroqProvider())
    assert client.tiers.strong == "my/strong"
    assert client.tiers.fast == "my/fast"


def test_defaults_do_not_reference_known_retired_models():
    """Groq retired llama-3.3-70b-versatile on 2026-08-16."""
    from codecrew.llm.client import DEFAULT_TIERS

    names = {m for t in DEFAULT_TIERS.values() for m in (t.fast, t.strong)}
    assert "llama-3.3-70b-versatile" not in names
    assert "gemini-2.0-flash" not in names
    assert "gemini-2.0-flash-lite" not in names


def test_http_404_is_not_retried_and_names_the_fix(monkeypatch):
    import io
    import urllib.error

    def raise_404(*args, **kwargs):
        raise urllib.error.HTTPError(
            "http://x", 404, "Not Found", {}, io.BytesIO(b'{"error":"gone"}'))

    monkeypatch.setattr("urllib.request.urlopen", raise_404)
    with pytest.raises(ProviderUnavailable) as exc:
        post_json("http://x", {})
    assert "CODECREW_MODEL_STRONG" in str(exc.value)
