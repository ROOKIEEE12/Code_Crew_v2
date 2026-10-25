"""``codecrew init``: set a repository up for day-to-day use.

Everything it writes is small, visible and reversible, and nothing that
already exists is overwritten: an existing ``[tool.codecrew]`` table, CI
workflow or pre-commit config is left alone and reported as such.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .ci_export import WORKFLOW_PATH, render_workflow

SETTINGS_BLOCK = """
[tool.codecrew]
# What CodeCrew checks before it calls this code verified. Full reference:
# https://github.com/ROOKIEEE12/Code_Crew_v2#configuration
profile = "full"              # "quick" skips mutation testing (never VERIFIED)
min-coverage = 70             # branch coverage, percent
min-mutation = 50             # share of injected bugs the tests must catch
exclude = []                  # globs, e.g. ["migrations/*", "scripts/*"]
install-dependencies = "auto" # install declared deps into an isolated env
"""

PRE_COMMIT_SNIPPET = """\
  - repo: https://github.com/ROOKIEEE12/Code_Crew_v2
    rev: main  # pin a tag or commit SHA
    hooks:
      - id: codecrew-quick
"""


@dataclass
class InitResult:
    written: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def init_project(root: Path, *, ci: bool = False, pre_commit: bool = False,
                 code_scanning: bool = False) -> InitResult:
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"not a directory: {root}")
    result = InitResult()

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8")
        if "[tool.codecrew]" in text:
            result.kept.append("pyproject.toml [tool.codecrew]")
        else:
            pyproject.write_text(text.rstrip("\n") + "\n" + SETTINGS_BLOCK,
                                 encoding="utf-8")
            result.written.append("pyproject.toml [tool.codecrew]")
    else:
        config = root / "codecrew.toml"
        if config.exists():
            result.kept.append("codecrew.toml")
        else:
            body = SETTINGS_BLOCK.replace("[tool.codecrew]\n", "", 1).lstrip("\n")
            config.write_text(body, encoding="utf-8")
            result.written.append("codecrew.toml")

    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if ".codecrew/" in existing.splitlines() or ".codecrew" in existing.splitlines():
        result.kept.append(".gitignore (.codecrew/)")
    else:
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        gitignore.write_text(
            existing + prefix + "# CodeCrew reports and evidence\n.codecrew/\n",
            encoding="utf-8")
        result.written.append(".gitignore (.codecrew/)")

    if ci:
        workflow = root / WORKFLOW_PATH
        if workflow.exists():
            result.kept.append(WORKFLOW_PATH.as_posix())
        else:
            workflow.parent.mkdir(parents=True, exist_ok=True)
            workflow.write_text(render_workflow(code_scanning=code_scanning),
                                encoding="utf-8")
            result.written.append(WORKFLOW_PATH.as_posix())

    if pre_commit:
        config = root / ".pre-commit-config.yaml"
        if config.exists():
            if "codecrew-quick" in config.read_text(encoding="utf-8"):
                result.kept.append(".pre-commit-config.yaml (codecrew-quick)")
            else:
                result.notes.append(
                    "Add this to the `repos:` list in .pre-commit-config.yaml:\n"
                    + PRE_COMMIT_SNIPPET)
        else:
            config.write_text("repos:\n" + PRE_COMMIT_SNIPPET, encoding="utf-8")
            result.written.append(".pre-commit-config.yaml")
            result.notes.append("Run `pre-commit install` to activate it.")
    return result
