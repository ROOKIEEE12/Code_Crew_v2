"""The multi-agent pipeline: isolation, the approval checkpoint, and the
properties the Phase 2 hypothesis depends on.

All offline, via the scripted provider. These prove the plumbing and the
isolation guarantees. They deliberately do not claim anything about whether
independent tests are *better* - that is a measured result, not a unit test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codecrew.agents import (
    ContextViolation,
    ImplementerAgent,
    Spec,
    SpecAgent,
    TestAuthorAgent,
)
from codecrew.config import GateConfig
from codecrew.llm import LLMClient, LLMConfig, ScriptedProvider
from codecrew.pipeline import Pipeline

REQ_MARKER = "REQUIREMENT-TEXT-MARKER-4471"
IMPL_MARKER = "IMPLEMENTATION-MARKER-7f3a"
TEST_MARKER = "TEST-MARKER-9c1d"

SPEC = {
    "summary": "Clamp a number into an inclusive range.",
    "module": "clamp",
    "functions": [{
        "signature": "clamp(value: float, low: float, high: float) -> float",
        "description": "Limit value to [low, high]; low > high raises ValueError.",
        "raises": ["ValueError: when low > high"],
        "examples": [
            {"call": "clamp(5, 0, 10)", "result": "5"},
            {"call": "clamp(1, 5, 0)", "raises": "ValueError"},
        ],
    }],
    "security": ["no untrusted input"],
    "assumptions": ["both bounds are inclusive"],
}

IMPLEMENTATION = f'''
# {IMPL_MARKER}
def clamp(value: float, low: float, high: float) -> float:
    """Limit value to [low, high]."""
    if low > high:
        raise ValueError("low must not exceed high")
    return max(low, min(value, high))
'''

TESTS = f'''
# {TEST_MARKER}
import pytest
from clamp import clamp


def test_inside():
    assert clamp(5, 0, 10) == 5


def test_inverted_range_raises():
    with pytest.raises(ValueError):
        clamp(1, 5, 0)
'''


def response_spec() -> str:
    return json.dumps(SPEC)


def response_impl() -> str:
    return json.dumps({"files": [{"path": "clamp.py", "content": IMPLEMENTATION}],
                       "notes": "n", "self_assessment": "unsure about floats"})


def response_tests() -> str:
    return json.dumps({"files": [{"path": "tests/test_clamp.py",
                                  "content": TESTS}], "notes": "n"})


def flatten(messages) -> str:
    return "\n".join(m.content for m in messages)


@pytest.fixture
def scripted(monkeypatch):
    def factory(responses: list[str]) -> ScriptedProvider:
        provider = ScriptedProvider(responses)
        monkeypatch.setattr(
            "codecrew.llm.client.select_provider",
            lambda name="auto", script=None: provider,
        )
        return provider
    return factory


def pipeline() -> Pipeline:
    return Pipeline(LLMConfig(provider="scripted", backoff_base_s=0.0),
                    GateConfig(sandbox="local"))


# -- structural isolation -----------------------------------------------------

def test_test_author_cannot_be_handed_the_implementation():
    client = LLMClient(LLMConfig(provider="scripted"),
                       provider=ScriptedProvider([response_tests()]))
    spec = Spec.from_dict(SPEC)
    with pytest.raises(ContextViolation, match="may not receive: code"):
        TestAuthorAgent(client).run(spec=spec, code=IMPLEMENTATION)


def test_implementer_cannot_be_handed_the_tests_or_the_raw_requirement():
    client = LLMClient(LLMConfig(provider="scripted"),
                       provider=ScriptedProvider([response_impl()]))
    spec = Spec.from_dict(SPEC)
    with pytest.raises(ContextViolation, match="tests"):
        ImplementerAgent(client).run(spec=spec, tests=TESTS)
    with pytest.raises(ContextViolation, match="requirement"):
        ImplementerAgent(client).run(spec=spec, requirement="anything")


def test_an_agent_missing_required_context_is_refused():
    client = LLMClient(LLMConfig(provider="scripted"),
                       provider=ScriptedProvider([response_spec()]))
    with pytest.raises(ContextViolation, match="requires: requirement"):
        SpecAgent(client).run()


def test_no_model_call_is_made_when_context_is_refused():
    provider = ScriptedProvider([response_tests()])
    client = LLMClient(LLMConfig(provider="scripted"), provider=provider)
    with pytest.raises(ContextViolation):
        TestAuthorAgent(client).run(spec=Spec.from_dict(SPEC), code="x")
    assert provider.calls == []


# -- isolation as observed in what the model actually received ------------------

@pytest.mark.slow
def test_each_agent_sees_only_the_spec_in_its_prompt(scripted, tmp_path: Path):
    provider = scripted([response_spec(), response_impl(), response_tests()])
    result = pipeline().build(f"{REQ_MARKER} clamp a number", tmp_path / "ws",
                              mode="multi", verify=False)

    assert result.failure is None, result.failure
    spec_call, impl_call, test_call = (flatten(c) for c in provider.calls)

    # Only the spec agent reads the raw requirement.
    assert REQ_MARKER in spec_call
    assert REQ_MARKER not in impl_call
    assert REQ_MARKER not in test_call

    # Neither downstream agent sees the other's work.
    assert TEST_MARKER not in impl_call
    assert IMPL_MARKER not in test_call

    # Both received the approved spec.
    assert '"module": "clamp"' in impl_call
    assert '"module": "clamp"' in test_call


def test_the_audit_trail_records_what_each_agent_received(scripted, tmp_path: Path):
    scripted([response_spec(), response_impl(), response_tests()])
    pipeline().build("clamp a number", tmp_path / "ws", mode="multi", verify=False)

    trace_file = next((tmp_path / "ws" / ".codecrew" / "traces").glob("*.jsonl"))
    events = [json.loads(line) for line in
              trace_file.read_text(encoding="utf-8").splitlines()]
    contexts = {e["agent"]: e["keys"] for e in events if e["kind"] == "agent_context"}

    assert contexts == {
        "spec": ["requirement"],
        "implementer": ["spec"],
        "test_author": ["spec"],
    }


# -- the approval checkpoint --------------------------------------------------

def test_a_rejected_spec_stops_the_run_before_any_code_is_written(
    scripted, tmp_path: Path
):
    provider = scripted([response_spec()])
    result = pipeline().build("clamp a number", tmp_path / "ws", mode="multi",
                              approve=lambda spec: None, verify=False)

    assert result.failure and "rejected" in result.failure
    assert result.submission is None
    assert len(provider.calls) == 1, "no implementer or test author call was made"
    assert not (tmp_path / "ws" / "clamp.py").exists()


def test_the_approver_sees_the_assumptions_and_may_edit_the_spec(
    scripted, tmp_path: Path
):
    provider = scripted([response_spec(), response_impl(), response_tests()])
    seen = {}

    def approve(spec: Spec) -> Spec:
        seen["assumptions"] = list(spec.assumptions)
        edited = Spec.from_dict({**spec.to_dict(),
                                 "constraints": ["EDITED-BY-HUMAN-5512"]})
        return edited

    result = pipeline().build("clamp a number", tmp_path / "ws", mode="multi",
                              approve=approve, verify=False)

    assert result.failure is None
    assert seen["assumptions"] == ["both bounds are inclusive"]
    # The human's edit, not the model's draft, is what the agents received.
    assert "EDITED-BY-HUMAN-5512" in flatten(provider.calls[1])
    assert "EDITED-BY-HUMAN-5512" in flatten(provider.calls[2])


def test_the_approved_spec_is_saved_with_the_workspace(scripted, tmp_path: Path):
    scripted([response_spec(), response_impl(), response_tests()])
    pipeline().build("clamp a number", tmp_path / "ws", mode="multi", verify=False)

    saved = tmp_path / "ws" / ".codecrew" / "spec.json"
    assert saved.exists()
    assert Spec.parse(saved.read_text(encoding="utf-8")).module == "clamp"


def test_a_supplied_spec_skips_the_spec_agent(scripted, tmp_path: Path):
    provider = scripted([response_impl(), response_tests()])
    result = pipeline().build("ignored", tmp_path / "ws", mode="multi",
                              spec=Spec.from_dict(SPEC), verify=False)

    assert result.failure is None
    assert len(provider.calls) == 2, "only implementer and test author ran"


# -- composition --------------------------------------------------------------

def test_multi_mode_writes_implementation_and_tests_from_separate_agents(
    scripted, tmp_path: Path
):
    scripted([response_spec(), response_impl(), response_tests()])
    result = pipeline().build("clamp a number", tmp_path / "ws", mode="multi",
                              verify=False)

    assert (tmp_path / "ws" / "clamp.py").exists()
    assert (tmp_path / "ws" / "tests" / "test_clamp.py").exists()
    assert result.mode == "multi"
    assert result.spec is not None and result.spec.module == "clamp"
    assert result.agent_attempts == 3


def test_contract_violations_in_either_agent_are_repaired(scripted, tmp_path: Path):
    # The implementer first smuggles in a test file; the repair must drop it.
    smuggled = json.dumps({"files": [
        {"path": "clamp.py", "content": IMPLEMENTATION},
        {"path": "tests/test_clamp.py", "content": TESTS}]})
    scripted([response_spec(), smuggled, response_impl(), response_tests()])
    result = pipeline().build("clamp a number", tmp_path / "ws", mode="multi",
                              verify=False)

    assert result.failure is None
    assert len(result.contract_rejections) == 1
    assert "test file" in result.contract_rejections[0]


def test_unknown_mode_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown mode"):
        pipeline().build("x", tmp_path / "ws", mode="swarm")


def test_single_mode_is_unchanged(scripted, tmp_path: Path):
    submission = json.dumps({"files": [
        {"path": "clamp.py", "content": IMPLEMENTATION},
        {"path": "tests/test_clamp.py", "content": TESTS}]})
    provider = scripted([submission])
    result = pipeline().build("clamp a number", tmp_path / "ws", mode="single",
                              verify=False)

    assert result.failure is None
    assert len(provider.calls) == 1
    assert result.spec is None


# -- end to end through the real gates ------------------------------------------

@pytest.mark.slow
def test_multi_mode_output_goes_through_the_same_gates(scripted, tmp_path: Path):
    scripted([response_spec(), response_impl(), response_tests()])
    config = GateConfig(sandbox="local", disabled=("G3", "G4", "G6"))
    result = Pipeline(LLMConfig(provider="scripted"), config).build(
        "clamp a number", tmp_path / "ws", mode="multi")

    assert result.report is not None
    by_id = {r.gate_id: r.status.value for r in result.report.results}
    assert by_id["G1"] == "PASS"
    assert by_id["G2"] == "PASS"
    assert by_id["G5"] == "PASS"
