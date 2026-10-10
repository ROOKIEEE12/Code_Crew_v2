"""Tests for the agent retry/repair loop and the LLM client."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codecrew.agents import AgentFailure, CoderAgent
from codecrew.llm import LLMClient, LLMConfig, Message, ScriptedProvider, TaskKind
from codecrew.llm.base import LLMError, RateLimited, Usage
from codecrew.llm.client import DEFAULT_TIERS, ModelTiers, select_provider
from codecrew.trace import Tracer

VALID = json.dumps({
    "files": [
        {"path": "app.py",
         "content": "def add(a: int, b: int) -> int:\n    \"\"\"Add.\"\"\"\n    return a + b\n"},
        {"path": "tests/test_app.py",
         "content": "from app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"},
    ],
    "notes": "ok",
})


def client_with(responses: list[str], tracer=None) -> LLMClient:
    provider = ScriptedProvider(responses)
    config = LLMConfig(provider="scripted", max_retries=2, backoff_base_s=0.0)
    return LLMClient(config, provider=provider, tracer=tracer)


# -- the repair loop ----------------------------------------------------------

def test_valid_output_on_first_attempt():
    agent = CoderAgent(client_with([VALID]))
    result = agent.run(requirement="add two numbers")
    assert result.attempts == 1
    assert result.rejections == []
    assert len(result.output.files) == 2


def test_malformed_output_is_repaired_on_retry():
    agent = CoderAgent(client_with(["I'm afraid I can't do that.", VALID]))
    result = agent.run(requirement="add two numbers")
    assert result.attempts == 2
    assert len(result.rejections) == 1
    assert "JSON" in result.rejections[0]


def test_the_specific_error_is_sent_back_to_the_model():
    """The retry must name the broken rule, not just ask again."""
    provider = ScriptedProvider([json.dumps({"notes": "forgot the files"}), VALID])
    config = LLMConfig(provider="scripted", max_retries=3, backoff_base_s=0.0)
    agent = CoderAgent(LLMClient(config, provider=provider))
    agent.run(requirement="add two numbers")

    second_call = provider.calls[1]
    feedback = second_call[-1].content
    assert second_call[-1].role == "user"
    assert "no 'files' key" in feedback
    # The rejected answer stays in the conversation as the assistant turn.
    assert second_call[-2].role == "assistant"


def test_agent_gives_up_after_its_budget_and_reports_why():
    agent = CoderAgent(client_with(["nope", "still nope"]))
    # The agent's repair budget is separate from the client's retry budget:
    # one counts contract violations, the other counts transport failures.
    agent.max_attempts = 2
    with pytest.raises(AgentFailure) as exc:
        agent.run(requirement="add two numbers")
    assert exc.value.attempts == 2
    assert len(exc.value.rejections) == 2


def test_agent_uses_its_full_repair_budget_before_failing():
    agent = CoderAgent(client_with(["no", "still no", "nope again"]))
    assert agent.max_attempts == 3
    with pytest.raises(AgentFailure) as exc:
        agent.run(requirement="add two numbers")
    assert len(exc.value.rejections) == 3


def test_truncated_code_triggers_a_repair_not_a_pass():
    truncated = json.dumps({
        "files": [{"path": "app.py", "content": "def add(a, b):\n    return a +"},
                  {"path": "tests/test_app.py", "content": "def test_x():\n    assert True\n"}],
    })
    agent = CoderAgent(client_with([truncated, VALID]))
    result = agent.run(requirement="add two numbers")
    assert result.attempts == 2
    assert "not valid Python" in result.rejections[0]


def test_requirement_must_not_be_empty():
    agent = CoderAgent(client_with([VALID]))
    with pytest.raises(ValueError, match="non-empty"):
        agent.run(requirement="   ")


# -- tracing ------------------------------------------------------------------

def test_every_call_and_rejection_is_traced(tmp_path: Path):
    tracer = Tracer(tmp_path / "trace.jsonl", run_id="t1")
    agent = CoderAgent(client_with(["bad", VALID], tracer=tracer), tracer=tracer)
    agent.run(requirement="add two numbers")

    kinds = [event["kind"] for event in tracer.read()]
    assert kinds.count("llm_call") == 2
    assert kinds.count("contract_rejected") == 1
    assert "agent_done" in kinds
    totals = tracer.totals()
    assert totals["llm_calls"] == 2


def test_totals_survive_a_run_with_no_calls(tmp_path: Path):
    tracer = Tracer(tmp_path / "trace.jsonl")
    assert tracer.totals()["llm_calls"] == 0


# -- routing ------------------------------------------------------------------

def test_reasoning_tasks_route_to_the_strong_tier():
    provider = ScriptedProvider(["ok", "ok"])
    client = LLMClient(
        LLMConfig(provider="scripted", tiers=ModelTiers(fast="small", strong="big")),
        provider=provider,
    )
    assert client.complete([Message("user", "x")], kind=TaskKind.FORMAT).model == "small"
    assert client.complete([Message("user", "x")], kind=TaskKind.DIAGNOSE).model == "big"


def test_every_provider_has_both_tiers_configured():
    for name, tiers in DEFAULT_TIERS.items():
        assert tiers.fast, f"{name} has no fast model"
        assert tiers.strong, f"{name} has no strong model"


def test_temperature_defaults_to_zero():
    """Determinism by default; diversity only when explicitly wanted."""
    assert LLMConfig().temperature == 0.0


# -- retries and failure modes ------------------------------------------------

class _FlakyProvider(ScriptedProvider):
    """Rate-limits once, then succeeds. Mirrors real free-tier behaviour."""

    def __init__(self, responses, failures: int = 1):
        super().__init__(responses)
        self.remaining_failures = failures

    def complete(self, messages, **kwargs):
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise RateLimited("slow down", retry_after_s=0.0)
        return super().complete(messages, **kwargs)


def test_rate_limits_are_retried_not_surfaced(tmp_path: Path):
    tracer = Tracer(tmp_path / "trace.jsonl")
    provider = _FlakyProvider([VALID])
    client = LLMClient(
        LLMConfig(provider="scripted", max_retries=3, backoff_base_s=0.0),
        provider=provider, tracer=tracer,
    )
    completion = client.complete([Message("user", "x")])
    assert completion.text == VALID
    retries = [e for e in tracer.read() if e["kind"] == "llm_retry"]
    assert len(retries) == 1
    assert retries[0]["reason"] == "rate_limited"


def test_persistent_failure_raises_after_the_retry_budget():
    provider = _FlakyProvider([VALID], failures=99)
    client = LLMClient(
        LLMConfig(provider="scripted", max_retries=2, backoff_base_s=0.0),
        provider=provider,
    )
    with pytest.raises(LLMError, match="failed after 2 attempts"):
        client.complete([Message("user", "x")])


def test_exhausted_scripted_provider_is_an_explicit_error():
    provider = ScriptedProvider([])
    client = LLMClient(LLMConfig(provider="scripted", max_retries=1,
                                 backoff_base_s=0.0), provider=provider)
    with pytest.raises(LLMError, match="exhausted"):
        client.complete([Message("user", "x")])


def test_truncated_completions_are_flagged(tmp_path: Path):
    """A response cut off at the token ceiling corrupts code silently."""
    class Truncating(ScriptedProvider):
        def complete(self, messages, **kwargs):
            completion = super().complete(messages, **kwargs)
            completion.finish_reason = "length"
            return completion

    tracer = Tracer(tmp_path / "trace.jsonl")
    client = LLMClient(LLMConfig(provider="scripted"),
                       provider=Truncating(["partial"]), tracer=tracer)
    client.complete([Message("user", "x")])
    assert any(e["kind"] == "llm_truncated" for e in tracer.read())


# -- provider selection -------------------------------------------------------

def test_unknown_provider_name_is_rejected():
    from codecrew.llm import ProviderUnavailable

    with pytest.raises(ProviderUnavailable, match="unknown provider"):
        select_provider("gpt5")


def test_auto_selection_never_picks_a_paid_provider(monkeypatch):
    """A default that silently spends money is the wrong default."""
    from codecrew.llm.client import AUTO_ORDER

    assert "anthropic" not in AUTO_ORDER


def test_auto_selection_explains_itself_when_nothing_is_available(monkeypatch):
    from codecrew.llm import ProviderUnavailable

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(
        "codecrew.llm.providers.OllamaProvider.available",
        lambda self: (False, "no Ollama server on localhost:11434"),
    )
    with pytest.raises(ProviderUnavailable) as exc:
        select_provider("auto")
    message = str(exc.value)
    assert "GROQ_API_KEY" in message
    assert "scripted" in message  # tells the user how to proceed offline


def test_scripted_provider_is_always_available():
    assert ScriptedProvider().available() == (True, "")


# -- offline replay -----------------------------------------------------------

def test_replay_script_of_plain_strings_loads(tmp_path: Path):
    path = tmp_path / "script.json"
    path.write_text(json.dumps(["first", "second"]), encoding="utf-8")
    provider = select_provider("scripted", str(path))
    assert provider.responses == ["first", "second"]


def test_replay_script_of_trace_objects_loads(tmp_path: Path):
    """A recorded trace can be replayed without reshaping it by hand."""
    path = tmp_path / "script.json"
    path.write_text(json.dumps([{"text": "a", "model": "x"}]), encoding="utf-8")
    assert select_provider("scripted", str(path)).responses == ["a"]


def test_missing_replay_script_is_reported_clearly(tmp_path: Path):
    from codecrew.llm import ProviderUnavailable

    with pytest.raises(ProviderUnavailable, match="not found"):
        select_provider("scripted", str(tmp_path / "absent.json"))


def test_malformed_replay_script_is_reported_clearly(tmp_path: Path):
    from codecrew.llm import ProviderUnavailable

    path = tmp_path / "script.json"
    path.write_text('{"not": "an array"}', encoding="utf-8")
    with pytest.raises(ProviderUnavailable, match="JSON array"):
        select_provider("scripted", str(path))


# -- response shape parsing ---------------------------------------------------

def test_openai_shape_is_parsed_with_usage():
    from codecrew.llm.providers import _from_openai_shape

    completion = _from_openai_shape(
        {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        },
        "groq", "llama-3.3-70b-versatile", 1.5,
    )
    assert completion.text == "hi"
    assert completion.usage.total == 13
    assert completion.finish_reason == "stop"


def test_empty_choices_is_an_error_not_an_empty_string():
    from codecrew.llm.providers import _from_openai_shape

    with pytest.raises(LLMError, match="no choices"):
        _from_openai_shape({"choices": []}, "groq", "m", 0.1)


def test_usage_total_is_none_when_unreported():
    assert Usage().total is None
    assert Usage(prompt_tokens=5).total == 5
