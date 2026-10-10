"""The kind of test suite an unsupervised generator writes.

It is green. It also constrains almost nothing: happy-path inputs only, no
boundary, no error path, and assertions loose enough that most mutants of
the implementation still pass.
"""

from strength import is_strong, score_password


def test_score_works():
    assert score_password("Abcdefgh1!") == 4


def test_strong_password_is_strong():
    assert is_strong("Abcdefgh1!")


def test_weak_password_is_not_strong():
    assert not is_strong("abc")
