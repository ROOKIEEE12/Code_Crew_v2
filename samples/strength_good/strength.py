"""Password strength scoring.

Reference 'good' workspace: no security findings, branch-covered, and its
tests pin behaviour tightly enough to kill most mutants.
"""

from __future__ import annotations

MIN_LENGTH = 8
SYMBOLS = "!@#$%^&*()-_=+[]{};:,.<>?/"
STRONG_SCORE = 3
MAX_SCORE = 4


def score_password(password: str) -> int:
    """Return a strength score from 0 to 4.

    One point each for sufficient length, a digit, an uppercase letter and
    a symbol.
    """
    score = 0
    if len(password) >= MIN_LENGTH:
        score += 1
    if any(char.isdigit() for char in password):
        score += 1
    if any(char.isupper() for char in password):
        score += 1
    if any(char in SYMBOLS for char in password):
        score += 1
    return score


def is_strong(password: str) -> bool:
    """Whether ``password`` meets the minimum acceptable score."""
    return score_password(password) >= STRONG_SCORE


def describe(password: str) -> str:
    """Human-readable verdict for a password."""
    score = score_password(password)
    if score >= STRONG_SCORE:
        return "strong"
    if score >= 2:
        return "moderate"
    return "weak"
