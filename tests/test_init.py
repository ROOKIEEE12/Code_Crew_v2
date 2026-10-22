"""`codecrew init` and the CI/pre-commit integration it sets up.

The rule for anything that writes into someone's repository: small,
visible, and never overwriting what is already there.
"""

from __future__ import annotations

from pathlib import Path

from codecrew import toml
from codecrew.__main__ import main as cli
from codecrew.ci_export import render_workflow
from codecrew.init import init_project
from codecrew.project import Project


def test_init_adds_config_and_ignores_the_report_directory(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    (tmp_path / ".gitignore").write_text("venv/", encoding="utf-8")
    result = init_project(tmp_path)
    assert "pyproject.toml [tool.codecrew]" in result.written
    data = toml.loads((tmp_path / "pyproject.toml").read_text(encoding="utf-8"),
                      force_fallback=True)
    assert data["tool"]["codecrew"]["profile"] == "full"
    # the written settings are valid settings
    assert Project.detect(tmp_path).settings["min-coverage"] == 70
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()[-1] == ".codecrew/"


def test_init_is_idempotent_and_never_overwrites(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.codecrew]\nprofile = "quick"\n', encoding="utf-8")
    first = init_project(tmp_path, ci=True)
    before = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    second = init_project(tmp_path, ci=True)
    assert "pyproject.toml [tool.codecrew]" in first.kept
    assert ".github/workflows/codecrew.yml" in first.written
    assert set(second.written) == set()
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == before


def test_without_a_pyproject_a_codecrew_toml_is_written(tmp_path: Path):
    init_project(tmp_path)
    assert Project.detect(tmp_path).settings_file == "codecrew.toml"


def test_pre_commit_config_is_created_or_explained(tmp_path: Path):
    result = init_project(tmp_path, pre_commit=True)
    assert "codecrew-quick" in (tmp_path / ".pre-commit-config.yaml").read_text(
        encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    (other / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    result = init_project(other, pre_commit=True)
    assert any("codecrew-quick" in n for n in result.notes)
    assert (other / ".pre-commit-config.yaml").read_text(encoding="utf-8") == "repos: []\n"


def test_code_scanning_is_opt_in():
    plain = render_workflow()
    assert "security-events" not in plain and "upload-sarif" not in plain
    scanning = render_workflow(code_scanning=True)
    assert "security-events: write" in scanning
    assert "--sarif codecrew.sarif" in scanning
    assert "github/codeql-action/upload-sarif@v3" in scanning
    # the environment cache keys on dependency declarations
    assert "hashFiles('**/pyproject.toml', '**/requirements*.txt')" in plain


def test_the_published_pre_commit_hooks_use_real_cli_flags():
    hooks = (Path(__file__).resolve().parents[1] / ".pre-commit-hooks.yaml").read_text(
        encoding="utf-8")
    assert "entry: codecrew verify . --profile quick --quiet" in hooks
    from codecrew.__main__ import build_parser
    build_parser().parse_args(["verify", ".", "--profile", "quick", "--quiet"])


def test_cli_init(tmp_path: Path, capsys):
    assert cli(["init", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "wrote  codecrew.toml" in out and "codecrew verify ." in out
    assert cli(["init", str(tmp_path / "missing")]) == 2
