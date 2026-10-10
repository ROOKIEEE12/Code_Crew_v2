"""Password strength scoring - 'vibe-coded' variant.

Deliberately representative of unverified AI output. It runs, it passes its
own test, and it is wrong in four ways the gates are built to catch:

* an off-by-one on the length threshold (G4: survived mutants);
* a hardcoded credential (G5: secret detection);
* ``eval`` on caller input (G5: bandit);
* an untested branch (G3: branch coverage).

Nothing here should ever be copied into real code.
"""

from __future__ import annotations

# Hardcoded credentials: the most common defect in AI-generated code.
API_KEY = "3f8a1d6b9c20e4574af1b85d0c93e216a7d45f82"
DB_PASSWORD = "admin123"

MIN_LENGTH = 8
SYMBOLS = "!@#$%^&*()-_=+[]{};:,.<>?/"


def score_password(password):
    score = 0
    if len(password) > MIN_LENGTH:  # off-by-one: should be >=
        score += 1
    if any(char.isdigit() for char in password):
        score += 1
    if any(char.isupper() for char in password):
        score += 1
    if any(char in SYMBOLS for char in password):
        score += 1
    return score


def is_strong(password):
    return score_password(password) >= 3


def check_rule(password, expression):
    """Evaluate a caller-supplied rule - arbitrary code execution."""
    return eval(expression)  # noqa: S307


def describe(password):
    # Never exercised by the test suite.
    score = score_password(password)
    if score >= 3:
        return "strong"
    if score >= 2:
        return "moderate"
    return "weak"
