"""A small, deterministic AST mutation engine.

Why we implement this instead of depending on an existing mutation tester:

1. The result is the project's central claim ("these tests detect real bugs"),
   so it should be inspectable rather than a black box.
2. Off-the-shelf runners are fragile on Windows and hard to bound in time.
3. We need per-mutant evidence - *which* surviving mutant was missed - to
   hand the Test-Author agent a concrete, actionable gap in Phase 2.

A mutant is a single small change to the code under test. If the test suite
still passes with the change applied, the mutant *survived*: the tests do not
actually constrain that behaviour. Mutation score is the percentage killed.
"""

from __future__ import annotations

import ast
import random
from dataclasses import dataclass
from pathlib import Path

#: Comparison operators swapped for their boundary-shifted counterpart.
#: These catch classic off-by-one errors that happy-path tests miss.
_COMPARE_SWAPS: dict[type, type] = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
}

_BINOP_SWAPS: dict[type, type] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
    ast.FloorDiv: ast.Div,
    ast.Mod: ast.Mult,
    ast.Pow: ast.Mult,
    ast.BitAnd: ast.BitOr,
    ast.BitOr: ast.BitAnd,
    ast.LShift: ast.RShift,
    ast.RShift: ast.LShift,
}

_OP_SYMBOL: dict[type, str] = {
    ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
    ast.Eq: "==", ast.NotEq: "!=", ast.In: "in", ast.NotIn: "not in",
    ast.Is: "is", ast.IsNot: "is not",
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
    ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**",
    ast.BitAnd: "&", ast.BitOr: "|", ast.LShift: "<<", ast.RShift: ">>",
    ast.And: "and", ast.Or: "or",
}


@dataclass(frozen=True)
class MutationSite:
    """One candidate change, identified by its ordinal within a file.

    The index is stable because the AST walk order is deterministic, so a
    surviving mutant can be named and re-checked after a patch.
    """

    file: str
    index: int
    operator: str
    lineno: int
    description: str

    @property
    def label(self) -> str:
        return f"{self.file}:{self.lineno} [{self.operator}] {self.description}"


class _Plan:
    """Collected candidate nodes for a single parse of a single file."""

    def __init__(self, tree: ast.Module, candidates: list[tuple[ast.AST, str, str]]):
        self.tree = tree
        self.candidates = candidates


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """Identify docstring constants so we never 'mutate' documentation."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and \
                isinstance(body[0].value, ast.Constant) and \
                isinstance(body[0].value.value, str):
            ids.add(id(body[0].value))
    return ids


def _raise_message_nodes(tree: ast.Module) -> set[int]:
    """Identify string literals used as exception messages.

    Mutating ``raise ValueError("too small")`` into ``raise ValueError("")``
    produces an *equivalent mutant*: the program's behaviour is unchanged
    except for human-readable text, so a well-written suite that asserts on
    the exception type legitimately does not catch it. Counting those as
    survivors understates the real mutation score, so they are excluded.

    Note this filter applies only to strings; mutating the *type* raised or
    the condition that triggers it is still a genuine mutation.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        for child in ast.walk(node.exc):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                ids.add(id(child))
    return ids


def _annotation_nodes(tree: ast.Module) -> set[int]:
    """Constants inside annotations are types, not behaviour."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        annotation = getattr(node, "annotation", None) or getattr(node, "returns", None)
        if annotation is not None:
            for child in ast.walk(annotation):
                ids.add(id(child))
    return ids


def plan_file(source: str) -> _Plan:
    """Parse ``source`` and enumerate every mutation candidate in walk order."""
    tree = ast.parse(source)
    skip = (
        _docstring_nodes(tree)
        | _annotation_nodes(tree)
        | _raise_message_nodes(tree)
    )
    candidates: list[tuple[ast.AST, str, str]] = []

    for node in ast.walk(tree):
        if id(node) in skip:
            continue

        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            op_type = type(node.ops[0])
            if op_type in _COMPARE_SWAPS:
                new = _COMPARE_SWAPS[op_type]
                candidates.append((
                    node, "comparison",
                    f"{_OP_SYMBOL[op_type]} -> {_OP_SYMBOL[new]}",
                ))

        elif isinstance(node, ast.BinOp) and type(node.op) in _BINOP_SWAPS:
            op_type = type(node.op)
            new = _BINOP_SWAPS[op_type]
            candidates.append((
                node, "arithmetic",
                f"{_OP_SYMBOL[op_type]} -> {_OP_SYMBOL[new]}",
            ))

        elif isinstance(node, ast.BoolOp):
            op_type = type(node.op)
            other = "or" if op_type is ast.And else "and"
            candidates.append((
                node, "logical", f"{_OP_SYMBOL[op_type]} -> {other}",
            ))

        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            candidates.append((node, "negation", "drop 'not'"))

        elif isinstance(node, ast.Constant):
            replacement = _mutate_constant(node.value)
            if replacement is not _NO_CHANGE:
                candidates.append((
                    node, "constant",
                    f"{node.value!r} -> {replacement!r}",
                ))

        elif isinstance(node, ast.Return) and node.value is not None:
            candidates.append((node, "return", "return <expr> -> return None"))

    return _Plan(tree, candidates)


class _NoChange:
    pass


_NO_CHANGE = _NoChange()


def _mutate_constant(value: object) -> object:
    """Pick a boundary-shifting replacement for a literal."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 1.0
    if isinstance(value, str):
        return "" if value else "codecrew"
    return _NO_CHANGE


def site_for(plan: _Plan, file: str, index: int) -> MutationSite:
    node, operator, description = plan.candidates[index]
    return MutationSite(
        file=file,
        index=index,
        operator=operator,
        lineno=getattr(node, "lineno", 0),
        description=description,
    )


def apply_mutation(plan: _Plan, index: int) -> str:
    """Apply one mutation to ``plan``'s tree and return the new source.

    The plan is consumed: callers re-parse for each mutant, which keeps the
    engine simple and side-effect free at the cost of one extra parse.
    """
    node, operator, _ = plan.candidates[index]

    if operator == "comparison" and isinstance(node, ast.Compare):
        node.ops = [_COMPARE_SWAPS[type(node.ops[0])]()]
    elif operator == "arithmetic" and isinstance(node, ast.BinOp):
        node.op = _BINOP_SWAPS[type(node.op)]()
    elif operator == "logical" and isinstance(node, ast.BoolOp):
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    elif operator == "constant" and isinstance(node, ast.Constant):
        node.value = _mutate_constant(node.value)
    elif operator in ("negation", "return"):
        # These replace a node rather than edit it in place, so they need a
        # transformer that matches on object identity.
        plan.tree = _Replacer(node, operator).visit(plan.tree)
    else:  # pragma: no cover - defensive
        raise ValueError(f"unknown mutation operator: {operator}")

    ast.fix_missing_locations(plan.tree)
    return ast.unparse(plan.tree)


class _Replacer(ast.NodeTransformer):
    """Replaces one specific node, matched by identity."""

    def __init__(self, target: ast.AST, operator: str) -> None:
        self.target = target
        self.operator = operator

    def visit(self, node: ast.AST) -> ast.AST:
        if node is self.target:
            if self.operator == "negation" and isinstance(node, ast.UnaryOp):
                return self.generic_visit(node.operand)
            if self.operator == "return" and isinstance(node, ast.Return):
                # Explicit None rather than a bare ``return``: identical in
                # behaviour, but the evidence log then matches the operator
                # description the report shows.
                return ast.Return(value=ast.Constant(value=None))
        return super().visit(node)


def collect_sites(workspace: Path, files: list[Path]) -> list[MutationSite]:
    """Enumerate every mutation site across the given source files."""
    sites: list[MutationSite] = []
    for rel in files:
        path = workspace / rel
        try:
            source = path.read_text(encoding="utf-8")
            plan = plan_file(source)
        except (OSError, SyntaxError):
            continue
        key = rel.as_posix()
        for index in range(len(plan.candidates)):
            sites.append(site_for(plan, key, index))
    return sites


def sample_sites(
    sites: list[MutationSite], limit: int, seed: int = 20261010
) -> list[MutationSite]:
    """Deterministically sample at most ``limit`` sites.

    Mutation testing is the slowest gate, so large files are sampled rather
    than exhaustively mutated. The fixed seed keeps scores comparable
    between runs, which matters for the ablation study.
    """
    if limit <= 0 or len(sites) <= limit:
        return list(sites)
    chosen = random.Random(seed).sample(range(len(sites)), limit)
    return [sites[i] for i in sorted(chosen)]


def mutated_source(workspace: Path, site: MutationSite) -> str:
    """Re-parse the file and return its source with ``site`` applied."""
    path = workspace / site.file
    plan = plan_file(path.read_text(encoding="utf-8"))
    return apply_mutation(plan, site.index)
