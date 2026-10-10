"""Tests that pin behaviour at its boundaries.

Written to be mutation-resistant: exact scores rather than ``> 0``, and both
sides of every threshold, so that shifting a comparison or a constant makes
a test fail.
"""

from __future__ import annotations

import pytest
from hypothesis import given, strategies as st

from strength import MAX_SCORE, describe, is_strong, score_password


@pytest.mark.parametrize(
    "password, expected",
    [
        ("", 0),
        ("abcdefg", 0),        # one character below the length threshold
        ("abcdefgh", 1),       # exactly at the length threshold
        ("abcdefgh1", 2),
        ("Abcdefgh1", 3),
        ("Abcdefgh1!", 4),
        ("A1!", 3),            # short but otherwise complete
    ],
)
def test_score_is_exact(password: str, expected: int) -> None:
    assert score_password(password) == expected


@pytest.mark.parametrize(
    "password, expected",
    [
        ("abcdefgh1", False),  # score 2: just below strong
        ("Abcdefgh1", True),   # score 3: exactly strong
        ("Abcdefgh1!", True),
    ],
)
def test_is_strong_at_the_boundary(password: str, expected: bool) -> None:
    assert is_strong(password) is expected


@pytest.mark.parametrize(
    "password, expected",
    [
        ("abcdefg", "weak"),
        ("abcdefgh1", "moderate"),
        ("Abcdefgh1", "strong"),
    ],
)
def test_describe(password: str, expected: str) -> None:
    assert describe(password) == expected


@given(st.text(max_size=40))
def test_score_stays_in_range(password: str) -> None:
    assert 0 <= score_password(password) <= MAX_SCORE


@given(st.text(max_size=40))
def test_is_strong_agrees_with_score(password: str) -> None:
    assert is_strong(password) == (score_password(password) >= 3)


@given(st.text(max_size=40))
def test_describe_is_consistent_with_is_strong(password: str) -> None:
    assert (describe(password) == "strong") == is_strong(password)
