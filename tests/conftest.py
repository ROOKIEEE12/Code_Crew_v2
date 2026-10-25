"""Shared test setup.

Tests must never read or write the developer's own ~/.codecrew/projects.json,
so CODECREW_HOME points every test at a throwaway directory. The gate tool
venv (CODECREW_VENV) is deliberately left alone: rebuilding it per test
would cost minutes.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_codecrew_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("CODECREW_HOME", str(tmp_path_factory.mktemp("codecrew-home")))
