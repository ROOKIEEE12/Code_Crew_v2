"""Verifying a developer's own repository, not a generated workspace.

Every test here comes from a false alarm or a hazard found by running
`codecrew verify` on an ordinary src-layout project with one third-party
dependency: mypy failing the build over missing type stubs, the
dependency check not reading pyproject.toml, tests unable to import
declared packages, and mutation testing editing the user's files in place.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codecrew import toml
from codecrew.config import GateConfig
from codecrew.gates import GateRunner
from codecrew.gates.base import GateContext, is_excluded_part
from codecrew.gates.dependencies import declared_packages
from codecrew.gates.syntax import _parse_mypy
from codecrew.project import Project, ProjectConfigError
from codecrew.results import Finding, GateReport, GateResult, Severity, Status
from codecrew.sarif import render_sarif

PYPROJECT = '''
[project]
name = "datekit"
dependencies = ["python-dateutil>=2.8", "attrs"]

[project.optional-dependencies]
test = ["freezegun==1.5.1"]
docs = ["sphinx"]

[tool.codecrew]
profile = "quick"
min-coverage = 85
disable = ["g6"]
exclude = ["scripts/*"]
'''


# -- TOML ----------------------------------------------------------------------

TOML_SAMPLE = '''
# comment
title = "x" # trailing
[a.b]
n = 3
f = 1.5
yes = true
list = [
  "one",  # inline comment
  'two',
]
inline = { k = "v", m = 2 }
[[items]]
id = 1
[[items]]
id = 2
'''


def test_fallback_toml_parser_matches_tomllib_semantics():
    data = toml.loads(TOML_SAMPLE, force_fallback=True)
    assert data["title"] == "x"
    assert data["a"]["b"] == {"n": 3, "f": 1.5, "yes": True,
                              "list": ["one", "two"], "inline": {"k": "v", "m": 2}}
    assert data["items"] == [{"id": 1}, {"id": 2}]


@pytest.mark.parametrize("bad", ['a = "unterminated', "a = nope", "a = 1\na = 2",
                                 "[t\nx=1", "a = [1, 2"])
def test_fallback_toml_parser_refuses_what_it_cannot_read(bad):
    with pytest.raises(toml.TomlError):
        toml.loads(bad, force_fallback=True)


def test_fallback_parser_reads_a_real_pyproject():
    root = Path(__file__).resolve().parents[1]
    data = toml.loads((root / "pyproject.toml").read_text(encoding="utf-8"),
                      force_fallback=True)
    assert data["project"]["name"] == "codecrew"
    assert data["project"]["scripts"]["codecrew"] == "codecrew.__main__:main"


# -- project detection -----------------------------------------------------------

@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (tmp_path / "src" / "datekit").mkdir(parents=True)
    (tmp_path / "src" / "datekit" / "__init__.py").write_text(
        "from dateutil.relativedelta import relativedelta\nimport attr\n",
        encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "release.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "build" / "lib").mkdir(parents=True)
    (tmp_path / "build" / "lib" / "old.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "requirements-dev.txt").write_text(
        "# tools\nrequests==2.32.3  # http\n-e .\n", encoding="utf-8")
    return tmp_path


def test_a_src_layout_project_is_understood(repo: Path):
    project = Project.detect(repo)
    assert project.source_root == "src"
    assert not project.generated
    assert project.should_install()
    # test extras count, documentation extras do not
    assert "freezegun==1.5.1" in project.dependencies
    assert "sphinx" not in project.dependencies
    assert project.requirement_files == ["requirements-dev.txt"]
    assert project.settings_file == "pyproject.toml [tool.codecrew]"


def test_settings_fold_into_the_gate_config(repo: Path):
    config = Project.detect(repo).apply(GateConfig())
    assert config.thresholds.min_branch_coverage == 85
    # quick profile adds G4; the gate list is normalised to upper case
    assert set(config.disabled) == {"G6", "G4"}


@pytest.mark.parametrize("snippet,fragment", [
    ('profile = "turbo"', "profile"),
    ("min-coverage = 120", "min-coverage"),
    ('disable = ["coverage"]', "disable"),
    ('install-dependencies = "sometimes"', "install-dependencies"),
    ("colour = 1", "unknown"),
])
def test_bad_settings_are_explained(tmp_path: Path, snippet, fragment):
    (tmp_path / "pyproject.toml").write_text(f"[tool.codecrew]\n{snippet}\n",
                                             encoding="utf-8")
    with pytest.raises(ProjectConfigError, match=fragment):
        Project.detect(tmp_path)


def test_generated_projects_do_not_install_model_chosen_packages(tmp_path: Path):
    (tmp_path / ".codecrew").mkdir()
    (tmp_path / ".codecrew" / "plan.json").write_text("{}", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("leftpad-ai\n", encoding="utf-8")
    project = Project.detect(tmp_path)
    assert project.generated and project.has_dependencies()
    assert not project.should_install()


def test_the_dependency_digest_follows_requirement_file_contents(repo: Path):
    before = Project.detect(repo).dependency_digest()
    (repo / "requirements-dev.txt").write_text("requests==2.32.4\n", encoding="utf-8")
    assert Project.detect(repo).dependency_digest() != before


class _Stub:
    name = "stub"
    strict = False

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def ensure(self) -> None:
        pass

    def has_module(self, module: str) -> bool:
        return False

    def run(self, args, *, timeout, cwd=None):
        raise AssertionError("must not execute")


def test_discovery_skips_build_output_and_configured_excludes(repo: Path):
    project = Project.detect(repo)
    ctx = GateContext.build(repo, _Stub(repo), GateConfig(), repo / "ev",
                            project=project)
    assert [p.as_posix() for p in ctx.source_files] == ["src/datekit/__init__.py"]
    # src layout: the importable name, not the directory, for coverage targets
    assert ctx.package_roots() == ["datekit"]
    assert is_excluded_part("datekit.egg-info") and is_excluded_part(".tox")


def test_dependencies_declared_in_pyproject_and_requirements_count(repo: Path):
    ctx = GateContext.build(repo, _Stub(repo), GateConfig(), repo / "ev",
                            project=Project.detect(repo))
    declared = declared_packages(ctx)
    assert {"python-dateutil", "attrs", "freezegun", "requests"} <= declared


def test_the_report_names_the_setup_it_ran_with(repo: Path):
    report = GateRunner(repo, GateConfig(), sandbox=_Stub(repo),
                        project=Project.detect(repo)).run(only=["G1"])
    assert report.sources == ["src/datekit/__init__.py"]
    assert "src layout: code under src/" in report.notes
    assert "configuration: pyproject.toml [tool.codecrew]" in report.notes


# -- G1 calibration ---------------------------------------------------------------------

def test_missing_type_stubs_are_not_a_code_defect():
    out = ('src/a.py:2: error: Library stubs not installed for "dateutil"  '
           '[import-untyped]\n'
           'src/a.py:9: error: Incompatible return value type (got "str", '
           'expected "int")  [return-value]\n')
    stubs, real = _parse_mypy(out)
    assert stubs.severity is Severity.LOW and stubs.code == "mypy:missing-stubs"
    assert real.severity is Severity.MEDIUM and real.code == "mypy:error"


# -- verdict policy -----------------------------------------------------------------------

def _gate(gate_id, status, blocking=True):
    return GateResult(gate_id=gate_id, name=gate_id, status=status, summary="s",
                      blocking=blocking)


def test_only_a_blocking_gate_that_did_not_run_lowers_the_verdict():
    advisory_skip = GateReport("r", "w", "s", [_gate("G2", Status.PASS),
                                               _gate("G6", Status.SKIP, False)])
    assert advisory_skip.verdict() == "VERIFIED"
    blocking_skip = GateReport("r", "w", "s", [_gate("G2", Status.PASS),
                                               _gate("G4", Status.SKIP)])
    assert blocking_skip.verdict() == "PARTIALLY VERIFIED"
    assert blocking_skip.passed, "a skip never blocks"


def test_a_disabled_blocking_gate_still_lowers_the_verdict(tmp_path: Path):
    """The quick profile must never be able to say VERIFIED."""
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    report = GateRunner(tmp_path, GateConfig(disabled=("G4",)),
                        sandbox=_Stub(tmp_path)).run(only=["G4"])
    g4 = report.results[0]
    assert g4.status is Status.SKIP and g4.blocking
    assert report.verdict() == "PARTIALLY VERIFIED"


# -- SARIF ---------------------------------------------------------------------------------

def test_sarif_locates_findings_relative_to_the_repo(tmp_path: Path):
    absolute = str((tmp_path / "src" / "a.py").resolve())
    report = GateReport("r", str(tmp_path), "s", [GateResult(
        gate_id="G1", name="Syntax", status=Status.FAIL, summary="s",
        findings=[Finding(Severity.HIGH, "ruff:F821", "undefined name 'x'",
                          file=absolute, line=4),
                  Finding(Severity.LOW, "mypy:missing-stubs", "stubs", file="src\\a.py")])])
    sarif = render_sarif(report, tmp_path)
    run = sarif["runs"][0]
    assert sarif["version"] == "2.1.0"
    first, second = run["results"]
    location = first["locations"][0]["physicalLocation"]
    assert location["artifactLocation"]["uri"] == "src/a.py"
    assert location["region"]["startLine"] == 4
    assert (first["level"], second["level"]) == ("error", "note")
    assert run["properties"]["verdict"] == "UNVERIFIED"
    json.dumps(sarif)  # serialisable


# -- end to end ----------------------------------------------------------------------------

DATEKIT = '''"""Small date helpers."""
from datetime import date

from dateutil.relativedelta import relativedelta


def add_months(day: date, months: int) -> date:
    return day + relativedelta(months=months)


def is_weekend(day: date) -> bool:
    return day.weekday() >= 5
'''

DATEKIT_TESTS = '''from datetime import date

from datekit import add_months, is_weekend


def test_add_months_clamps():
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)


def test_add_months_back():
    assert add_months(date(2024, 3, 15), -2) == date(2024, 1, 15)


def test_weekend():
    assert is_weekend(date(2024, 6, 1))
    assert not is_weekend(date(2024, 6, 3))
'''


@pytest.mark.slow
def test_a_real_project_with_a_dependency_is_verified_end_to_end(tmp_path: Path):
    """Needs network for the dependency install (cached afterwards)."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "datekit"\ndependencies = ["python-dateutil>=2.8"]\n',
        encoding="utf-8")
    (tmp_path / "src" / "datekit").mkdir(parents=True)
    source = tmp_path / "src" / "datekit" / "__init__.py"
    source.write_text(DATEKIT, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_datekit.py").write_text(DATEKIT_TESTS, encoding="utf-8")
    before = source.stat().st_mtime_ns

    project = Project.detect(tmp_path)
    report = GateRunner(tmp_path, GateConfig(sandbox="local"), project=project).run()
    statuses = {r.gate_id: r.status for r in report.results}
    if any("could not be installed" in n for n in report.notes):
        pytest.skip("no network for the dependency install: " + report.notes[0])
    assert statuses["G1"] is Status.PASS
    assert statuses["G8"] in (Status.PASS, Status.SKIP)  # SKIP = PyPI offline
    assert statuses["G2"] is Status.PASS
    assert statuses["G4"] is Status.PASS
    # Mutation testing ran on a copy: the user's file was never touched.
    assert source.stat().st_mtime_ns == before
    assert not list(tmp_path.rglob("*.codecrew-bak"))
    assert any("installed in an isolated environment" in n for n in report.notes)


def test_project_env_is_skipped_when_nothing_is_declared(tmp_path: Path):
    from codecrew.sandbox.local import LocalSandbox
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    box = LocalSandbox(tmp_path, venv_dir=tmp_path / "no-venv",
                       project=Project.detect(tmp_path))
    box._ensure_project_env()
    assert box.env_dir is None and box.dependencies_ready


def test_a_src_layout_is_put_on_the_import_path(repo: Path):
    from codecrew.sandbox.local import LocalSandbox
    box = LocalSandbox(repo, project=Project.detect(repo))
    assert box._env()["PYTHONPATH"] == str(repo.resolve() / "src")
    clone = box.for_workspace(repo / "copy")
    assert clone._env()["PYTHONPATH"] == str((repo / "copy").resolve() / "src")
