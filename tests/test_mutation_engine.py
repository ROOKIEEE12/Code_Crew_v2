"""Tests for the mutation engine.

The engine decides the project's headline metric, so a bug here would
silently invalidate every mutation score we report.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from codecrew import mutation as engine


def sites_for(source: str) -> list[tuple[str, str]]:
    plan = engine.plan_file(source)
    return [(op, desc) for _, op, desc in plan.candidates]


def mutate(source: str, index: int) -> str:
    return engine.apply_mutation(engine.plan_file(source), index)


def find(source: str, operator: str) -> int:
    plan = engine.plan_file(source)
    for i, (_, op, _) in enumerate(plan.candidates):
        if op == operator:
            return i
    raise AssertionError(f"no {operator} site in source")


# -- operator coverage --------------------------------------------------------

def test_comparison_is_boundary_shifted():
    index = find("def f(a, b):\n    return a < b\n", "comparison")
    mutated = mutate("def f(a, b):\n    return a < b\n", index)
    assert "a <= b" in mutated


def test_arithmetic_is_swapped():
    source = "def f(a, b):\n    return a + b\n"
    mutated = mutate(source, find(source, "arithmetic"))
    assert "a - b" in mutated


def test_logical_operator_is_swapped():
    source = "def f(a, b):\n    return a and b\n"
    mutated = mutate(source, find(source, "logical"))
    assert " or " in mutated


def test_negation_is_dropped():
    source = "def f(a):\n    return not a\n"
    mutated = mutate(source, find(source, "negation"))
    assert "not" not in mutated


def test_return_value_becomes_none():
    source = "def f(a):\n    return a * 2\n"
    mutated = mutate(source, find(source, "return"))
    assert "return None" in mutated


@pytest.mark.parametrize(
    "literal, expected",
    [("1", "2"), ("True", "False"), ("False", "True"), ("'x'", "''")],
)
def test_constants_are_shifted(literal: str, expected: str):
    source = f"def f():\n    x = {literal}\n    return x\n"
    mutated = mutate(source, find(source, "constant"))
    assert expected in mutated


# -- exclusions ---------------------------------------------------------------

def test_docstrings_are_never_mutated():
    source = '''def f():
    """A docstring."""
    return 1
'''
    descriptions = [desc for op, desc in sites_for(source) if op == "constant"]
    assert not any("docstring" in d for d in descriptions)
    assert "A docstring." in mutate(source, find(source, "constant"))


def test_annotations_are_never_mutated():
    source = "def f(a: int = 1) -> int:\n    return a\n"
    # The default value is behaviour and may be mutated; the annotations are
    # types and must not be.
    for index, (op, _) in enumerate(sites_for(source)):
        if op == "constant":
            assert "-> int" in mutate(source, index)


# -- invariants ---------------------------------------------------------------

def test_every_mutant_is_valid_python():
    source = Path("samples/strength_good/strength.py").read_text(encoding="utf-8")
    plan = engine.plan_file(source)
    assert len(plan.candidates) > 10
    for index in range(len(plan.candidates)):
        ast.parse(mutate(source, index))  # raises on malformed output


def test_each_mutant_changes_the_source():
    source = Path("samples/strength_good/strength.py").read_text(encoding="utf-8")
    baseline = ast.unparse(ast.parse(source))
    plan = engine.plan_file(source)
    for index in range(len(plan.candidates)):
        assert mutate(source, index) != baseline, f"mutant {index} was a no-op"


def test_sampling_is_deterministic_and_bounded():
    sites = [
        engine.MutationSite("a.py", i, "constant", i, f"{i} -> {i + 1}")
        for i in range(100)
    ]
    first = engine.sample_sites(sites, 10)
    second = engine.sample_sites(sites, 10)
    assert len(first) == 10
    assert first == second, "scores must be comparable between runs"
    assert first == sorted(first, key=lambda s: s.index)


def test_sampling_returns_everything_when_under_the_limit():
    sites = [engine.MutationSite("a.py", i, "constant", i, "x") for i in range(5)]
    assert engine.sample_sites(sites, 40) == sites


def test_collect_skips_unparseable_files(tmp_path: Path):
    (tmp_path / "broken.py").write_text("def f(:\n", encoding="utf-8")
    (tmp_path / "fine.py").write_text("def g():\n    return 1\n", encoding="utf-8")
    sites = engine.collect_sites(tmp_path, [Path("broken.py"), Path("fine.py")])
    assert {s.file for s in sites} == {"fine.py"}
