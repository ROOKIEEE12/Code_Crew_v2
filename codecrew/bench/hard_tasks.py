"""Harder benchmark tasks (Phase 6).

Both pipelines reached 10/10 on the hidden tests of the original suite, so
it could not separate them on correctness. These four are chosen for rules
that are easy to state and easy to get subtly wrong, which is where a model
grading its own work is most likely to be confidently wrong:

* ``semver``    SemVer 2.0.0 precedence: numeric vs alphanumeric pre-release
                identifiers, leading zeros, build metadata ignored.
* ``csv_line``  RFC 4180 quoting: doubled quotes, commas inside quotes, and
                exactly which malformed lines must be rejected.
* ``expr_eval`` operator precedence and associativity with unary minus,
                written without ``eval`` (checked, not trusted).
* ``token_bucket`` a stateful rate limiter driven by an injected clock:
                refill caps, fractional accumulation, a clock going back.

They are kept out of the default ``bench`` suite so that every earlier
number stays comparable; ``codecrew eval`` includes them.
"""

from __future__ import annotations

from .tasks import Task, _req

HARD_TASKS: tuple[Task, ...] = (
    Task(
        id="semver",
        tier="hard",
        module="semver_compare",
        requirement=_req(
            "Compare two Semantic Versioning 2.0.0 version strings by "
            "precedence. A version is MAJOR.MINOR.PATCH, optionally followed "
            "by '-' and a pre-release (dot-separated identifiers), optionally "
            "followed by '+' and build metadata (dot-separated identifiers). "
            "Identifiers contain only ASCII letters, digits and '-', and must "
            "not be empty. MAJOR, MINOR, PATCH and purely numeric pre-release "
            "identifiers must not have leading zeros (build metadata may). "
            "Any invalid version raises ValueError. Precedence: compare MAJOR, "
            "MINOR and PATCH numerically; a version with a pre-release has "
            "lower precedence than the same version without one; pre-release "
            "identifiers are compared left to right, numeric identifiers "
            "numerically and alphanumeric ones in ASCII order, with numeric "
            "identifiers lower than alphanumeric ones; if all compared "
            "identifiers are equal, the longer pre-release is higher. Build "
            "metadata is ignored. Return -1 if a < b, 0 if they have equal "
            "precedence, 1 if a > b.",
            "semver_compare",
            "- `compare_versions(a: str, b: str) -> int`",
        ),
        hidden_tests=r'''
import pytest
from semver_compare import compare_versions

def test_core_parts_compare_numerically_not_as_text():
    assert compare_versions("1.0.0", "2.0.0") == -1
    assert compare_versions("2.1.1", "2.1.0") == 1
    assert compare_versions("1.10.0", "1.9.0") == 1
    assert compare_versions("1.0.0", "1.0.0") == 0

CHAIN = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
         "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]

@pytest.mark.parametrize("i", range(len(CHAIN) - 1))
def test_the_specification_example_chain(i):
    assert compare_versions(CHAIN[i], CHAIN[i + 1]) == -1
    assert compare_versions(CHAIN[i + 1], CHAIN[i]) == 1

def test_numeric_identifiers_compare_numerically():
    assert compare_versions("1.0.0-alpha.10", "1.0.0-alpha.9") == 1

def test_numeric_identifiers_are_lower_than_alphanumeric_ones():
    assert compare_versions("1.0.0-1", "1.0.0-a") == -1
    assert compare_versions("1.0.0-0a", "1.0.0-1") == 1

def test_build_metadata_is_ignored():
    assert compare_versions("1.0.0+build.1", "1.0.0+exp.sha.5114f85") == 0
    assert compare_versions("1.0.0-alpha+001", "1.0.0-alpha") == 0
    assert compare_versions("1.0.0+001", "1.0.0") == 0

def test_hyphens_are_identifier_characters():
    assert compare_versions("1.0.0-x-y-z.--", "1.0.0-x-y-z.--") == 0
    assert compare_versions("1.0.0-x-y-z", "1.0.0") == -1

@pytest.mark.parametrize("bad", [
    "", "1.0", "1.0.0.0", "01.0.0", "1.01.0", "1.0.01", "1.0.0-", "1.0.0-01",
    "1.0.0-alpha..1", "1.0.0+", "1.0.0+build..1", "v1.0.0", "1.0.0-al$pha",
    " 1.0.0", "1.0.0 ", "1.0.0-alpha_1", "a.b.c", "-1.0.0",
])
def test_invalid_versions_raise_on_either_side(bad):
    with pytest.raises(ValueError):
        compare_versions(bad, "1.0.0")
    with pytest.raises(ValueError):
        compare_versions("1.0.0", bad)
''',
        reference='''
import re

_NUMBER = re.compile(r"0|[1-9][0-9]*")
_IDENT = re.compile(r"[0-9A-Za-z-]+")
_DIGITS = re.compile(r"[0-9]+")


def _parse(version):
    if not isinstance(version, str):
        raise ValueError("version must be a string")
    main, plus, build = version.partition("+")
    if plus and not all(_IDENT.fullmatch(p) for p in build.split(".")):
        raise ValueError(f"invalid build metadata in {version!r}")
    core, dash, pre = main.partition("-")
    parts = core.split(".")
    if len(parts) != 3 or not all(_NUMBER.fullmatch(p) for p in parts):
        raise ValueError(f"invalid version core in {version!r}")
    identifiers = []
    if dash:
        identifiers = pre.split(".")
        for ident in identifiers:
            if not _IDENT.fullmatch(ident):
                raise ValueError(f"invalid pre-release in {version!r}")
            if _DIGITS.fullmatch(ident) and not _NUMBER.fullmatch(ident):
                raise ValueError(f"leading zero in {version!r}")
    return tuple(int(p) for p in parts), identifiers


def _compare_pre(a, b):
    for x, y in zip(a, b):
        if x == y:
            continue
        x_num, y_num = _DIGITS.fullmatch(x), _DIGITS.fullmatch(y)
        if x_num and y_num:
            return -1 if int(x) < int(y) else 1
        if x_num:
            return -1
        if y_num:
            return 1
        return -1 if x < y else 1
    return (len(a) > len(b)) - (len(a) < len(b))


def compare_versions(a: str, b: str) -> int:
    core_a, pre_a = _parse(a)
    core_b, pre_b = _parse(b)
    if core_a != core_b:
        return -1 if core_a < core_b else 1
    if not pre_a or not pre_b:
        return (not pre_a and bool(pre_b)) - (bool(pre_a) and not pre_b)
    return _compare_pre(pre_a, pre_b)
''',
    ),
    Task(
        id="csv_line",
        tier="hard",
        module="csv_line",
        requirement=_req(
            "Split one line of CSV text into its fields, following RFC 4180, "
            "without using the csv module. Fields are separated by commas. A "
            "field may be enclosed in double quotes; inside a quoted field "
            "commas are literal and two consecutive double quotes stand for "
            "one double-quote character. Unquoted fields are taken verbatim, "
            "including any spaces. Raise ValueError when a double quote "
            "appears inside an unquoted field, when a quoted field is not "
            "closed, when anything other than a comma or the end of the line "
            "follows a closing quote, or when the line contains a carriage "
            "return or a newline. The empty string is a single empty field, "
            "and a trailing comma means a trailing empty field.",
            "csv_line",
            "- `parse_csv_line(line: str) -> list[str]`",
        ),
        hidden_tests=r'''
import builtins

import pytest
from csv_line import parse_csv_line

@pytest.mark.parametrize("line,fields", [
    ("a,b,c", ["a", "b", "c"]),
    ("", [""]),
    ("a,,c", ["a", "", "c"]),
    ("a,", ["a", ""]),
    (",", ["", ""]),
    (" a , b ", [" a ", " b "]),
    ('"a,b",c', ["a,b", "c"]),
    ('"say ""hi""",x', ['say "hi"', "x"]),
    ('""', [""]),
    ('"",""', ["", ""]),
    ('""""', ['"']),
    ('x,"y"', ["x", "y"]),
    ('" "', [" "]),
    ('"a",', ["a", ""]),
])
def test_fields(line, fields):
    assert parse_csv_line(line) == fields

@pytest.mark.parametrize("line", [
    'a"b', '"abc', '"a"b', '"a" ,b', "a\nb", "a\rb", '"a\nb"', 'x,"y', '"""',
])
def test_malformed_lines_raise(line):
    with pytest.raises(ValueError):
        parse_csv_line(line)

def test_does_not_lean_on_the_csv_module(monkeypatch):
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "csv":
            raise AssertionError("the csv module must not be used")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    import importlib, sys
    sys.modules.pop("csv_line", None)
    module = importlib.import_module("csv_line")
    assert module.parse_csv_line('"a,b",c') == ["a,b", "c"]
''',
        reference='''
def parse_csv_line(line: str) -> list[str]:
    if "\\n" in line or "\\r" in line:
        raise ValueError("a CSV line must not contain line breaks")
    fields = []
    i, n = 0, len(line)
    while True:
        if i < n and line[i] == '"':
            i += 1
            buf = []
            while True:
                if i >= n:
                    raise ValueError("unterminated quoted field")
                ch = line[i]
                if ch == '"':
                    if i + 1 < n and line[i + 1] == '"':
                        buf.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(ch)
                i += 1
            fields.append("".join(buf))
            if i == n:
                return fields
            if line[i] != ",":
                raise ValueError("unexpected text after a closing quote")
            i += 1
        else:
            j = line.find(",", i)
            end = n if j == -1 else j
            field = line[i:end]
            if '"' in field:
                raise ValueError("quote inside an unquoted field")
            fields.append(field)
            if j == -1:
                return fields
            i = j + 1
        if i == n:
            fields.append("")
            return fields
''',
    ),
    Task(
        id="expr_eval",
        tier="hard",
        module="expr_eval",
        requirement=_req(
            "Evaluate an arithmetic expression given as a string. Write the "
            "parser yourself: do not use eval, exec, compile or the ast "
            "module. Supported: non-negative integer literals (ASCII digits "
            "only), the binary operators + - * /, unary minus and unary plus, "
            "and parentheses. * and / bind tighter than + and -; binary "
            "operators of equal precedence associate left to right; unary "
            "operators bind tighter than every binary operator and may be "
            "repeated ('--3' is 3). Whitespace between tokens is ignored. "
            "Arithmetic follows Python's rules: + - * on integers give an "
            "int, and / is true division and gives a float. Division by zero "
            "raises ZeroDivisionError. Any other malformed input (empty, an "
            "unknown character, unbalanced parentheses, a missing operand, "
            "two numbers in a row, a decimal point, '**') raises ValueError.",
            "expr_eval",
            "- `evaluate(expression: str) -> int | float`",
        ),
        hidden_tests=r'''
import builtins

import pytest
from expr_eval import evaluate

@pytest.mark.parametrize("text,value", [
    ("1+2*3", 7), ("(1+2)*3", 9), ("2-3-4", -5), ("2*-3", -6), ("--3", 3),
    ("-(2+3)", -5), ("+4", 4), (" 12 +  3 ", 15), ("2--3", 5), ("1-+1", 0),
    ("((((7))))", 7), ("-2*-2", 4), ("100", 100), ("0", 0),
    ("2*3+4*5", 26), ("2*(3+4)*5", 70), ("7-(2-1)", 6),
])
def test_integer_results(text, value):
    result = evaluate(text)
    assert result == value
    assert type(result) is int

@pytest.mark.parametrize("text,value", [
    ("10/4", 2.5), ("8/4/2", 1.0), ("6/3", 2.0), ("1+2*3-4/2", 5.0),
    ("-9/2", -4.5), ("2/(1+1)", 1.0),
])
def test_division_gives_floats(text, value):
    result = evaluate(text)
    assert result == pytest.approx(value)
    assert type(result) is float

@pytest.mark.parametrize("text", ["1/0", "1/(2-2)", "5/(3*0)"])
def test_division_by_zero(text):
    with pytest.raises(ZeroDivisionError):
        evaluate(text)

@pytest.mark.parametrize("text", [
    "", "   ", "1+", "*1", "(1+2", "1+2)", "()", "1 2", "1.5", "2**3",
    "__import__('os')", "1e3", "abc", "3 % 2", "(1)(2)", "4/", "1,000",
])
def test_malformed_input_raises_value_error(text):
    with pytest.raises(ValueError):
        evaluate(text)

def test_no_eval_exec_compile(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("eval/exec/compile must not be used")
    for name in ("eval", "exec", "compile"):
        monkeypatch.setattr(builtins, name, forbidden)
    assert evaluate("1+2*(3-1)") == 5
''',
        reference='''
_OPS = "+-*/()"


def _tokenize(text):
    tokens = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \\t":
            i += 1
        elif "0" <= ch <= "9":
            j = i
            while j < n and "0" <= text[j] <= "9":
                j += 1
            tokens.append(int(text[i:j]))
            i = j
        elif ch in _OPS:
            tokens.append(ch)
            i += 1
        else:
            raise ValueError(f"unexpected character {ch!r}")
    return tokens


def evaluate(expression: str):
    tokens = _tokenize(expression)
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def take():
        nonlocal pos
        token = peek()
        pos += 1
        return token

    def expr():
        value = term()
        while peek() in ("+", "-"):
            op = take()
            rhs = term()
            value = value + rhs if op == "+" else value - rhs
        return value

    def term():
        value = unary()
        while peek() in ("*", "/"):
            op = take()
            rhs = unary()
            value = value * rhs if op == "*" else value / rhs
        return value

    def unary():
        if peek() in ("+", "-"):
            op = take()
            value = unary()
            return -value if op == "-" else +value
        return primary()

    def primary():
        token = take()
        if isinstance(token, int):
            return token
        if token == "(":
            value = expr()
            if take() != ")":
                raise ValueError("missing closing parenthesis")
            return value
        raise ValueError(f"expected a number or '(', got {token!r}")

    value = expr()
    if pos != len(tokens):
        raise ValueError(f"unexpected token {tokens[pos]!r}")
    return value
''',
    ),
    Task(
        id="token_bucket",
        tier="hard",
        module="token_bucket",
        requirement=_req(
            "A token-bucket rate limiter. `TokenBucket(rate, capacity, "
            "clock=time.monotonic)`: `rate` is the number of tokens added per "
            "second and must be a positive int or float; `capacity` is the "
            "maximum number of tokens and must be a positive int; anything "
            "else (including bool) raises ValueError. The bucket starts full. "
            "`clock` is a zero-argument function returning the current time "
            "in seconds. `allow(cost=1)` first refills the bucket for the "
            "time elapsed since the previous refill (never above capacity; "
            "fractional tokens accumulate), then, if at least `cost` tokens "
            "are available, removes `cost` tokens and returns True; otherwise "
            "it returns False and removes nothing. `cost` must be a positive "
            "int or float not greater than capacity (not bool), otherwise "
            "ValueError. If the clock goes backwards, no tokens are added and "
            "none are lost. `tokens` is a read-only property giving the "
            "current number of tokens after refilling.",
            "token_bucket",
            "- `class TokenBucket` with `__init__(self, rate, capacity, "
            "clock=time.monotonic)`, `allow(self, cost=1) -> bool` and a "
            "read-only `tokens` property",
        ),
        hidden_tests=r'''
import pytest
from token_bucket import TokenBucket


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def test_starts_full_and_then_refuses():
    bucket = TokenBucket(1, 3, clock=Clock())
    assert [bucket.allow() for _ in range(4)] == [True, True, True, False]

def test_refills_with_time():
    clock = Clock()
    bucket = TokenBucket(1, 2, clock=clock)
    assert bucket.allow() and bucket.allow()
    clock.now = 0.5
    assert bucket.allow() is False
    clock.now = 1.0
    assert bucket.allow() is True
    assert bucket.allow() is False

def test_refill_never_exceeds_capacity():
    clock = Clock()
    bucket = TokenBucket(10, 2, clock=clock)
    clock.now = 100.0
    assert bucket.tokens == pytest.approx(2)
    assert [bucket.allow() for _ in range(3)] == [True, True, False]

def test_fractional_tokens_accumulate_across_refusals():
    clock = Clock()
    bucket = TokenBucket(1, 1, clock=clock)
    assert bucket.allow()
    for t in (0.3, 0.6, 0.9):
        clock.now = t
        assert bucket.allow() is False
    clock.now = 1.0
    assert bucket.allow() is True

def test_a_refused_request_removes_nothing():
    clock = Clock()
    bucket = TokenBucket(0.001, 5, clock=clock)
    assert bucket.allow(2)
    assert bucket.allow(4) is False
    assert bucket.tokens == pytest.approx(3)
    assert bucket.allow(3) is True

def test_fractional_costs():
    bucket = TokenBucket(1, 1, clock=Clock())
    assert bucket.allow(0.5) and bucket.allow(0.5)
    assert bucket.allow(0.5) is False

def test_rate_scales_refill():
    clock = Clock()
    bucket = TokenBucket(4, 4, clock=clock)
    for _ in range(4):
        assert bucket.allow()
    clock.now = 0.5
    assert bucket.tokens == pytest.approx(2)

def test_a_clock_going_backwards_adds_and_loses_nothing():
    clock = Clock(10.0)
    bucket = TokenBucket(1, 5, clock=clock)
    assert bucket.allow(3)
    clock.now = 5.0
    assert bucket.tokens == pytest.approx(2)
    assert bucket.allow(3) is False
    assert bucket.allow(2) is True

def test_tokens_is_read_only():
    bucket = TokenBucket(1, 1, clock=Clock())
    with pytest.raises(AttributeError):
        bucket.tokens = 5

def test_default_clock_works():
    assert TokenBucket(1, 1).allow() is True

@pytest.mark.parametrize("rate,capacity", [
    (0, 1), (-1, 1), (1, 0), (1, -2), (1, 1.5), (True, 1), (1, True),
    ("1", 1), (1, "3"), (None, 1),
])
def test_invalid_construction(rate, capacity):
    with pytest.raises(ValueError):
        TokenBucket(rate, capacity, clock=Clock())

@pytest.mark.parametrize("cost", [0, -1, 6, True, "1", None])
def test_invalid_cost(cost):
    bucket = TokenBucket(1, 5, clock=Clock())
    with pytest.raises(ValueError):
        bucket.allow(cost)
''',
        reference='''
import time


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class TokenBucket:
    def __init__(self, rate, capacity, clock=time.monotonic):
        if not _is_number(rate) or rate <= 0:
            raise ValueError("rate must be a positive number")
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        self._rate = rate
        self._capacity = capacity
        self._clock = clock
        self._tokens = float(capacity)
        self._last = clock()

    def _refill(self):
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last = now

    @property
    def tokens(self):
        self._refill()
        return self._tokens

    def allow(self, cost=1):
        if not _is_number(cost) or cost <= 0 or cost > self._capacity:
            raise ValueError("cost must be a positive number within capacity")
        self._refill()
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False
''',
    ),
)
