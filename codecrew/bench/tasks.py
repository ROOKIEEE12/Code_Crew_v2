"""Benchmark tasks with hidden tests and reference solutions.

Why self-authored tasks rather than SWE-bench: SWE-bench Verified is
saturated (the Vals.ai leaderboard stopped running it) and it measures
repository repair, whereas CodeCrew generates from a requirement.

Every task carries:

* a **requirement** that pins the module name and signatures, so a hidden
  test can import the result without guessing;
* **hidden tests** the agent never sees. They, not the agent's own tests,
  define "correct" - which is what makes false confidence measurable;
* a **reference** solution, used only to prove the hidden tests are
  satisfiable. A benchmark whose tests nobody has passed is not a benchmark.

Tiers: ``easy`` (one function, a few boundaries), ``medium`` (several
interacting rules), ``security`` (the failure modes AI code reliably gets
wrong: path traversal and injection).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    id: str
    tier: str
    module: str
    requirement: str
    hidden_tests: str
    reference: str


def _req(description: str, module: str, interface: str) -> str:
    return (
        f"{description}\n\n"
        f"Implement it in a module named `{module}` (the file `{module}.py` "
        f"at the workspace root) that exposes exactly:\n{interface}"
    )


TASKS: tuple[Task, ...] = (
    Task(
        id="fizzbuzz",
        tier="easy",
        module="fizzbuzz",
        requirement=_req(
            "Classic FizzBuzz as a list. Numbers 1..n; multiples of 3 give "
            "'Fizz', of 5 give 'Buzz', of both give 'FizzBuzz', otherwise the "
            "number as a string. n == 0 gives an empty list; negative n "
            "raises ValueError.",
            "fizzbuzz",
            "- `fizzbuzz(n: int) -> list[str]`",
        ),
        hidden_tests=r'''
import pytest
from fizzbuzz import fizzbuzz

def test_first_fifteen():
    assert fizzbuzz(15) == ["1","2","Fizz","4","Buzz","Fizz","7","8","Fizz",
                            "Buzz","11","Fizz","13","14","FizzBuzz"]

def test_zero_is_empty():
    assert fizzbuzz(0) == []

def test_one():
    assert fizzbuzz(1) == ["1"]

def test_negative_raises():
    with pytest.raises(ValueError):
        fizzbuzz(-1)

def test_thirty_is_fizzbuzz():
    assert fizzbuzz(30)[-1] == "FizzBuzz"
''',
        reference='''
def fizzbuzz(n: int) -> list[str]:
    if n < 0:
        raise ValueError("n must be non-negative")
    out = []
    for i in range(1, n + 1):
        if i % 15 == 0:
            out.append("FizzBuzz")
        elif i % 3 == 0:
            out.append("Fizz")
        elif i % 5 == 0:
            out.append("Buzz")
        else:
            out.append(str(i))
    return out
''',
    ),
    Task(
        id="palindrome",
        tier="easy",
        module="palindrome",
        requirement=_req(
            "Decide whether text is a palindrome, ignoring case and "
            "ignoring every character that is not a letter or digit. The "
            "empty string and strings with no letters or digits are "
            "palindromes.",
            "palindrome",
            "- `is_palindrome(text: str) -> bool`",
        ),
        hidden_tests=r'''
import pytest
from palindrome import is_palindrome

@pytest.mark.parametrize("text", [
    "A man, a plan, a canal: Panama", "", "   ", "No 'x' in Nixon", "Aa", "a",
    "!!!", "12321",
])
def test_palindromes(text):
    assert is_palindrome(text) is True

@pytest.mark.parametrize("text", ["race a car", "ab", "0P", "hello"])
def test_non_palindromes(text):
    assert is_palindrome(text) is False
''',
        reference='''
def is_palindrome(text: str) -> bool:
    cleaned = [c.lower() for c in text if c.isalnum()]
    return cleaned == cleaned[::-1]
''',
    ),
    Task(
        id="clamp",
        tier="easy",
        module="clamp",
        requirement=_req(
            "Clamp a number into an inclusive range. If low is greater than "
            "high, raise ValueError. Works for ints and floats.",
            "clamp",
            "- `clamp(value: float, low: float, high: float) -> float`",
        ),
        hidden_tests=r'''
import pytest
from clamp import clamp

@pytest.mark.parametrize("value,low,high,expected", [
    (5, 0, 10, 5), (-1, 0, 10, 0), (11, 0, 10, 10), (0, 0, 10, 0),
    (10, 0, 10, 10), (1.5, 0, 1, 1), (5, 3, 3, 3), (-7.5, -5, 5, -5),
])
def test_clamp(value, low, high, expected):
    assert clamp(value, low, high) == expected

def test_inverted_range_raises():
    with pytest.raises(ValueError):
        clamp(1, 5, 0)
''',
        reference='''
def clamp(value: float, low: float, high: float) -> float:
    if low > high:
        raise ValueError("low must not exceed high")
    return max(low, min(value, high))
''',
    ),
    Task(
        id="roman",
        tier="easy",
        module="roman",
        requirement=_req(
            "Roman numerals for 1..3999. to_roman converts an int; values "
            "outside 1..3999 (and non-ints, including bool) raise "
            "ValueError. from_roman parses canonical uppercase numerals "
            "only: anything that is not exactly the canonical form (such as "
            "'IIII', 'VX', 'IC', lowercase, or an empty string) raises "
            "ValueError.",
            "roman",
            "- `to_roman(n: int) -> str`\n- `from_roman(s: str) -> int`",
        ),
        hidden_tests=r'''
import pytest
from roman import from_roman, to_roman

@pytest.mark.parametrize("n,s", [
    (1, "I"), (4, "IV"), (9, "IX"), (14, "XIV"), (40, "XL"), (90, "XC"),
    (400, "CD"), (1994, "MCMXCIV"), (3999, "MMMCMXCIX"), (2024, "MMXXIV"),
])
def test_known_values(n, s):
    assert to_roman(n) == s
    assert from_roman(s) == n

@pytest.mark.parametrize("n", [0, -5, 4000, 10**6])
def test_out_of_range_raises(n):
    with pytest.raises(ValueError):
        to_roman(n)

def test_non_int_raises():
    for bad in (True, 3.0, "3", None):
        with pytest.raises(ValueError):
            to_roman(bad)

def test_round_trip_everywhere():
    for n in range(1, 4000):
        assert from_roman(to_roman(n)) == n

@pytest.mark.parametrize("s", ["IIII", "VX", "IC", "", "mcm", "ABC", "IIX"])
def test_non_canonical_raises(s):
    with pytest.raises(ValueError):
        from_roman(s)
''',
        reference='''
_PAIRS = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
          (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
          (5, "V"), (4, "IV"), (1, "I")]
_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def to_roman(n: int) -> str:
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 3999:
        raise ValueError("n must be an int in 1..3999")
    out = []
    for value, symbol in _PAIRS:
        while n >= value:
            out.append(symbol)
            n -= value
    return "".join(out)


def from_roman(s: str) -> int:
    if not isinstance(s, str) or not s:
        raise ValueError("not a roman numeral")
    total = 0
    for i, ch in enumerate(s):
        if ch not in _VALUES:
            raise ValueError("not a roman numeral")
        value = _VALUES[ch]
        if i + 1 < len(s) and _VALUES.get(s[i + 1], 0) > value:
            total -= value
        else:
            total += value
    if total < 1 or total > 3999 or to_roman(total) != s:
        raise ValueError("not a canonical roman numeral")
    return total
''',
    ),
    Task(
        id="wordfreq",
        tier="medium",
        module="wordfreq",
        requirement=_req(
            "Word frequency counting. A word is a maximal run of ASCII "
            "letters (A-Z, a-z); everything else separates words, so "
            "digits and apostrophes split or vanish ('it's' is the words "
            "'it' and 's'). Counting is case-insensitive and keys are "
            "lowercase. `top_n` returns (word, count) tuples sorted by "
            "count descending, then word ascending, truncated to n. n == 0 "
            "gives an empty list; negative n raises ValueError.",
            "wordfreq",
            "- `word_frequencies(text: str) -> dict[str, int]`\n"
            "- `top_n(text: str, n: int) -> list[tuple[str, int]]`",
        ),
        hidden_tests=r'''
import pytest
from wordfreq import top_n, word_frequencies

def test_case_insensitive_counts():
    assert word_frequencies("The the THE cat. Cat dog!") == {
        "the": 3, "cat": 2, "dog": 1}

def test_apostrophe_splits_words():
    assert word_frequencies("it's") == {"it": 1, "s": 1}

def test_digits_are_separators():
    assert word_frequencies("abc123def") == {"abc": 1, "def": 1}

def test_empty_text():
    assert word_frequencies("") == {}
    assert word_frequencies("123 !!! 456") == {}

def test_ties_break_alphabetically():
    assert top_n("b a b a c", 2) == [("a", 2), ("b", 2)]

def test_top_n_zero_and_oversized():
    assert top_n("b a b a c", 0) == []
    assert len(top_n("b a b a c", 10)) == 3

def test_top_n_orders_by_count_first():
    assert top_n("x y y z z z", 3) == [("z", 3), ("y", 2), ("x", 1)]

def test_negative_n_raises():
    with pytest.raises(ValueError):
        top_n("a", -1)
''',
        reference='''
import re

_WORD = re.compile(r"[A-Za-z]+")


def word_frequencies(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for word in _WORD.findall(text):
        key = word.lower()
        counts[key] = counts.get(key, 0) + 1
    return counts


def top_n(text: str, n: int) -> list[tuple[str, int]]:
    if n < 0:
        raise ValueError("n must be non-negative")
    items = sorted(word_frequencies(text).items(), key=lambda kv: (-kv[1], kv[0]))
    return items[:n]
''',
    ),
    Task(
        id="intervals",
        tier="medium",
        module="intervals",
        requirement=_req(
            "Merge integer intervals. Each interval is a (start, end) pair "
            "with start <= end, otherwise raise ValueError. Overlapping "
            "AND touching intervals (one ends exactly where the next "
            "starts) are merged. Return a new list of (start, end) tuples "
            "sorted by start; never modify the input. Empty input gives "
            "an empty list.",
            "intervals",
            "- `merge_intervals(intervals: list[tuple[int, int]]) -> "
            "list[tuple[int, int]]`",
        ),
        hidden_tests=r'''
import pytest
from intervals import merge_intervals

def test_empty():
    assert merge_intervals([]) == []

def test_classic_example():
    assert merge_intervals([(1, 3), (2, 6), (8, 10), (15, 18)]) == [
        (1, 6), (8, 10), (15, 18)]

def test_touching_intervals_merge():
    assert merge_intervals([(1, 4), (4, 5)]) == [(1, 5)]

def test_unsorted_input():
    assert merge_intervals([(5, 6), (1, 2)]) == [(1, 2), (5, 6)]

def test_contained_interval():
    assert merge_intervals([(1, 10), (2, 3)]) == [(1, 10)]

def test_single_and_point_intervals():
    assert merge_intervals([(3, 3)]) == [(3, 3)]
    assert merge_intervals([(1, 1), (1, 1)]) == [(1, 1)]

def test_input_is_not_mutated():
    data = [(5, 6), (1, 5), (9, 9)]
    snapshot = list(data)
    merge_intervals(data)
    assert data == snapshot

def test_lists_are_accepted_and_tuples_returned():
    assert merge_intervals([[1, 3], [2, 4]]) == [(1, 4)]

def test_inverted_interval_raises():
    with pytest.raises(ValueError):
        merge_intervals([(3, 1)])
''',
        reference='''
def merge_intervals(intervals):
    cleaned = []
    for start, end in intervals:
        if start > end:
            raise ValueError("interval start exceeds end")
        cleaned.append((start, end))
    cleaned.sort()
    merged = []
    for start, end in cleaned:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
''',
    ),
    Task(
        id="rpn",
        tier="medium",
        module="rpn",
        requirement=_req(
            "Evaluate a Reverse Polish Notation expression of "
            "whitespace-separated tokens: decimal numbers and the "
            "operators + - * /. Always return a float. Division by zero "
            "raises ZeroDivisionError. An unknown token, an operator with "
            "too few operands, an empty expression, or leftover operands "
            "raises ValueError.",
            "rpn",
            "- `evaluate_rpn(expression: str) -> float`",
        ),
        hidden_tests=r'''
import pytest
from rpn import evaluate_rpn

def test_simple_addition():
    assert evaluate_rpn("3 4 +") == 7.0

def test_compound_expression():
    assert evaluate_rpn("5 1 2 + 4 * + 3 -") == 14.0

def test_true_division():
    assert evaluate_rpn("2 3 /") == pytest.approx(2 / 3)
    assert evaluate_rpn("10 2 /") == 5.0

def test_operand_order_for_subtraction_and_division():
    assert evaluate_rpn("10 4 -") == 6.0
    assert evaluate_rpn("9 3 /") == 3.0

def test_negative_and_decimal_numbers():
    assert evaluate_rpn("-3 2 *") == -6.0
    assert evaluate_rpn("1.5 2 *") == 3.0

def test_extra_whitespace():
    assert evaluate_rpn("  4   2  *  ") == 8.0

def test_result_is_float():
    assert isinstance(evaluate_rpn("2 2 +"), float)

def test_division_by_zero():
    with pytest.raises(ZeroDivisionError):
        evaluate_rpn("1 0 /")

@pytest.mark.parametrize("expr", ["+", "1 +", "1 2", "", "   ", "1 x +", "1 2 3 +"])
def test_malformed_raises_value_error(expr):
    with pytest.raises(ValueError):
        evaluate_rpn(expr)
''',
        reference='''
_OPS = {"+", "-", "*", "/"}


def evaluate_rpn(expression: str) -> float:
    stack: list[float] = []
    for token in expression.split():
        if token in _OPS:
            if len(stack) < 2:
                raise ValueError("operator without enough operands")
            b = stack.pop()
            a = stack.pop()
            if token == "+":
                stack.append(a + b)
            elif token == "-":
                stack.append(a - b)
            elif token == "*":
                stack.append(a * b)
            else:
                if b == 0:
                    raise ZeroDivisionError("division by zero")
                stack.append(a / b)
        else:
            try:
                stack.append(float(token))
            except ValueError:
                raise ValueError(f"invalid token: {token!r}") from None
    if len(stack) != 1:
        raise ValueError("malformed expression")
    return stack[0]
''',
    ),
    Task(
        id="password_policy",
        tier="medium",
        module="password_policy",
        requirement=_req(
            "Validate a password against a policy and return the list of "
            "violated rule codes, in this fixed order: 'too_short' (fewer "
            "than 12 characters), 'no_digit', 'no_upper', 'no_lower', "
            "'no_symbol' (no character that is neither alphanumeric nor "
            "whitespace). A fully compliant password returns an empty "
            "list. A non-str argument raises TypeError.",
            "password_policy",
            "- `check_password(password: str) -> list[str]`",
        ),
        hidden_tests=r'''
import pytest
from password_policy import check_password

def test_compliant_password():
    assert check_password("Correct-Horse-9") == []

def test_everything_missing_in_order():
    assert check_password("") == [
        "too_short", "no_digit", "no_upper", "no_lower", "no_symbol"]

def test_short_lowercase_word():
    assert check_password("short") == [
        "too_short", "no_digit", "no_upper", "no_symbol"]

def test_length_boundary():
    assert check_password("Abcdefgh1!x") == ["too_short"]      # 11 chars
    assert check_password("Abcdefgh1!xy") == []                # 12 chars

def test_whitespace_is_not_a_symbol():
    assert check_password("Abcdefgh 1xyz") == ["no_symbol"]

def test_only_lowercase_letters_of_length_twelve():
    assert check_password("abcdefghijkl") == ["no_digit", "no_upper", "no_symbol"]

def test_missing_lowercase_only():
    assert check_password("ABCDEFGH1!XY") == ["no_lower"]

@pytest.mark.parametrize("bad", [None, 123, ["a"], b"abc"])
def test_non_string_raises_type_error(bad):
    with pytest.raises(TypeError):
        check_password(bad)
''',
        reference='''
def check_password(password: str) -> list[str]:
    if not isinstance(password, str):
        raise TypeError("password must be a str")
    violations = []
    if len(password) < 12:
        violations.append("too_short")
    if not any(c.isdigit() for c in password):
        violations.append("no_digit")
    if not any(c.isupper() for c in password):
        violations.append("no_upper")
    if not any(c.islower() for c in password):
        violations.append("no_lower")
    if not any(not c.isalnum() and not c.isspace() for c in password):
        violations.append("no_symbol")
    return violations
''',
    ),
    Task(
        id="safe_path",
        tier="security",
        module="safe_path",
        requirement=_req(
            "Safely join a user-supplied relative path onto a base "
            "directory, as a file server must before opening a file. "
            "Return the resolved absolute path (symlinks resolved, as "
            "os.path.realpath would). Raise ValueError if the user path "
            "is absolute, contains a NUL byte, or would resolve to "
            "anywhere outside the base directory (for example via '..'). "
            "A '..' that stays inside the base is allowed. An empty user "
            "path resolves to the base itself.",
            "safe_path",
            "- `safe_join(base: str, user_path: str) -> str`",
        ),
        hidden_tests=r'''
import os
import pytest
from safe_path import safe_join

def real(*parts):
    return os.path.realpath(os.path.join(*parts))

def test_simple_relative_path(tmp_path):
    base = str(tmp_path)
    assert safe_join(base, "a/b.txt") == real(base, "a", "b.txt")

def test_empty_path_is_the_base(tmp_path):
    assert safe_join(str(tmp_path), "") == real(str(tmp_path))

def test_dotdot_that_stays_inside_is_allowed(tmp_path):
    base = str(tmp_path)
    assert safe_join(base, "a/../b.txt") == real(base, "b.txt")
    assert safe_join(base, "a/b/../c.txt") == real(base, "a", "c.txt")

@pytest.mark.parametrize("bad", [
    "../x", "../../etc/passwd", "a/../../x", "..", "a/b/../../../x",
])
def test_traversal_is_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        safe_join(str(tmp_path), bad)

def test_absolute_path_is_rejected(tmp_path):
    outside = os.path.realpath(os.path.join(str(tmp_path), os.pardir, "elsewhere"))
    with pytest.raises(ValueError):
        safe_join(str(tmp_path), outside)

def test_nul_byte_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        safe_join(str(tmp_path), "a\x00b")

def test_sibling_directory_with_shared_prefix_is_rejected(tmp_path):
    base = tmp_path / "data"
    sibling = tmp_path / "data-private"
    base.mkdir()
    sibling.mkdir()
    # A naive startswith() containment check wrongly accepts this one.
    with pytest.raises(ValueError):
        safe_join(str(base), "../data-private/secret.txt")
''',
        reference='''
import os


def safe_join(base: str, user_path: str) -> str:
    if "\\x00" in user_path:
        raise ValueError("NUL byte in path")
    if os.path.isabs(user_path) or user_path.startswith(("/", "\\\\")):
        raise ValueError("absolute paths are not allowed")
    base_real = os.path.realpath(base)
    candidate = os.path.realpath(os.path.join(base_real, user_path))
    if os.path.commonpath([base_real, candidate]) != base_real:
        raise ValueError("path escapes the base directory")
    return candidate
''',
    ),
    Task(
        id="sql_builder",
        tier="security",
        module="sql_builder",
        requirement=_req(
            "Build a parameterised SELECT statement. Table and column "
            "names must each match [A-Za-z_][A-Za-z0-9_]* entirely, "
            "otherwise raise ValueError; so must filter keys. Columns "
            "must be non-empty. Values are never placed in the SQL text: "
            "each filter becomes `<key> = ?` and its value goes into the "
            "params list, in insertion order, joined by ' AND '. With no "
            "filters (None or empty) there is no WHERE clause. The SQL "
            "format is exactly `SELECT a, b FROM t WHERE x = ? AND y = ?`. "
            "Return a (sql, params) tuple.",
            "sql_builder",
            "- `build_select(table: str, columns: list[str], "
            "filters: dict | None = None) -> tuple[str, list]`",
        ),
        hidden_tests=r'''
import pytest
from sql_builder import build_select

def test_no_filters():
    assert build_select("users", ["id", "name"]) == (
        "SELECT id, name FROM users", [])
    assert build_select("users", ["id"], {}) == ("SELECT id FROM users", [])

def test_filters_are_parameterised_in_order():
    assert build_select("users", ["id"], {"age": 30, "name": "bob"}) == (
        "SELECT id FROM users WHERE age = ? AND name = ?", [30, "bob"])

def test_injection_in_a_value_stays_in_params():
    payload = "x'; DROP TABLE users;--"
    sql, params = build_select("users", ["id"], {"name": payload})
    assert "DROP" not in sql
    assert payload not in sql
    assert params == [payload]

@pytest.mark.parametrize("table", [
    "users; DROP TABLE x", "", "1users", "users\n", "us ers", "users--", "a.b",
])
def test_bad_table_names_raise(table):
    with pytest.raises(ValueError):
        build_select(table, ["id"])

@pytest.mark.parametrize("column", ["id, password", "", "*", "id\n", "a b"])
def test_bad_column_names_raise(column):
    with pytest.raises(ValueError):
        build_select("users", [column])

def test_bad_filter_key_raises():
    with pytest.raises(ValueError):
        build_select("users", ["id"], {"1=1 OR a": 1})

def test_empty_columns_raise():
    with pytest.raises(ValueError):
        build_select("users", [])
''',
        reference='''
import re

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _check(name) -> None:
    if not isinstance(name, str) or not _IDENT.fullmatch(name):
        raise ValueError(f"invalid identifier: {name!r}")


def build_select(table, columns, filters=None):
    _check(table)
    if not columns:
        raise ValueError("columns must not be empty")
    for column in columns:
        _check(column)
    sql = f"SELECT {', '.join(columns)} FROM {table}"
    params = []
    if filters:
        clauses = []
        for key, value in filters.items():
            _check(key)
            clauses.append(f"{key} = ?")
            params.append(value)
        sql += " WHERE " + " AND ".join(clauses)
    return sql, params
''',
    ),
)


def get_tasks(ids: list[str] | None = None) -> list[Task]:
    """Return all tasks, or the named subset in the order requested."""
    if not ids:
        return list(TASKS)
    by_id = {task.id: task for task in TASKS}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise KeyError(
            f"unknown task id(s): {', '.join(unknown)}; "
            f"known: {', '.join(by_id)}"
        )
    return [by_id[i] for i in ids]
