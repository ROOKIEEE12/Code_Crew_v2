"""Phase 4: the project plan, the dependency gate, and multi-module builds.

Offline throughout: PyPI is replaced by a stub and the model by the scripted
provider, so what is asserted is the logic - ordering, interface hand-off,
isolation, what blocks - not network or model behaviour.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from codecrew.agents import ContractError, ProjectPlan, Spec
from codecrew.bench import PROJECT_TASKS, TASKS, get_tasks
from codecrew.config import GateConfig
from codecrew.gates import GateRunner
from codecrew.gates import dependencies as deps
from codecrew.gates.base import GateContext
from codecrew.gates.dependencies import (
    DependencyGate,
    NetworkUnavailable,
    PackageMissing,
)
from codecrew.llm import LLMConfig, ScriptedProvider
from codecrew.pipeline import Pipeline, owner_module
from codecrew.results import Finding, GateReport, GateResult, Severity, Status

# -- a two-module plan ----------------------------------------------------------

def module(name, depends=(), requirement=None):
    return {"name": name, "responsibility": f"{name} things",
            "requirement": requirement or f"Provide the {name} module.",
            "depends_on": list(depends)}


PLAN = {
    "summary": "Text statistics.",
    "modules": [module("wordstats", ["tokenizer"]), module("tokenizer")],
    "dependencies": [],
}


def plan_with(**overrides) -> str:
    data = json.loads(json.dumps(PLAN))
    data.update(overrides)
    return json.dumps(data)


# -- the plan contract ----------------------------------------------------------

def test_a_valid_plan_parses_and_orders_dependencies_first():
    plan = ProjectPlan.parse(json.dumps(PLAN))
    assert [m.name for m in plan.modules] == ["wordstats", "tokenizer"]
    assert [m.name for m in plan.build_order()] == ["tokenizer", "wordstats"]


def test_build_order_is_stable_for_independent_modules():
    plan = ProjectPlan.from_dict({
        "summary": "s", "modules": [module("zeta"), module("alpha"),
                                    module("mid", ["zeta"])]})
    assert [m.name for m in plan.build_order()] == ["zeta", "alpha", "mid"]


def test_a_dependency_cycle_is_rejected_with_the_way_out():
    cyclic = [module("a", ["b"]), module("b", ["a"])]
    with pytest.raises(ContractError) as exc:
        ProjectPlan.parse(plan_with(modules=cyclic))
    assert "circular" in exc.value.message
    assert "shared part" in (exc.value.hint or "")


def test_an_unknown_or_self_dependency_is_rejected():
    with pytest.raises(ContractError, match="unknown module 'ghost'"):
        ProjectPlan.parse(plan_with(modules=[module("a", ["ghost"])]))
    with pytest.raises(ContractError, match="depends on itself"):
        ProjectPlan.parse(plan_with(modules=[module("a", ["a"])]))


@pytest.mark.parametrize("name", ["json", "random", "logging", "statistics"])
def test_a_module_named_like_the_standard_library_is_rejected(name):
    """A file called random.py silently replaces the real one everywhere."""
    with pytest.raises(ContractError, match="shadow the standard library"):
        ProjectPlan.parse(plan_with(modules=[module(name)]))


def test_the_same_rule_protects_single_module_specs():
    spec = {"summary": "s", "module": "json", "functions": [{
        "signature": "f(x)", "description": "d", "examples": []}]}
    with pytest.raises(ContractError, match="shadow"):
        Spec.from_dict(spec)


def test_module_count_and_duplicates_are_bounded():
    many = [module(f"m{i}") for i in range(9)]
    with pytest.raises(ContractError, match="limit is 8"):
        ProjectPlan.parse(plan_with(modules=many))
    with pytest.raises(ContractError, match="planned twice"):
        ProjectPlan.parse(plan_with(modules=[module("a"), module("a")]))


def test_a_third_party_dependency_must_state_its_purpose():
    with pytest.raises(ContractError) as exc:
        ProjectPlan.parse(plan_with(dependencies=[{"name": "requests"}]))
    assert "purpose" in exc.value.message
    assert "standard library" in (exc.value.hint or "")


@pytest.mark.parametrize("bad", ["", "-x", "has space", "a;rm -rf"])
def test_package_names_are_validated(bad):
    with pytest.raises(ContractError, match="invalid package name"):
        ProjectPlan.parse(plan_with(
            dependencies=[{"name": bad, "purpose": "p"}]))


def test_render_flags_third_party_packages_for_the_reviewer():
    rendered = ProjectPlan.parse(plan_with(
        dependencies=[{"name": "requests", "purpose": "http"}])).render()
    assert "CHECK THESE" in rendered and "requests: http" in rendered
    assert "standard library only" in ProjectPlan.parse(
        json.dumps(PLAN)).render()


# -- the dependency gate --------------------------------------------------------

class _NoSandbox:
    name, strict = "stub", False

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def ensure(self) -> None: ...
    def has_module(self, module: str) -> bool: return True
    def run(self, *a, **k): raise AssertionError("G8 must not execute code")


def run_g8(ws: Path):
    ctx = GateContext.build(ws, _NoSandbox(ws), GateConfig(),
                            ws / ".codecrew" / "evidence")
    return DependencyGate().execute(ctx)


def declare(ws: Path, *names: str) -> None:
    (ws / ".codecrew").mkdir(exist_ok=True)
    (ws / ".codecrew" / "plan.json").write_text(json.dumps({
        "dependencies": [{"name": n, "purpose": "p"} for n in names]}),
        encoding="utf-8")


def pypi(**fields) -> dict:
    uploaded = datetime.now(timezone.utc) - timedelta(days=fields.pop("age", 800))
    data = {"releases": {"1.0": [{"upload_time_iso_8601":
                                  uploaded.isoformat().replace("+00:00", "Z")}]},
            "vulnerabilities": fields.pop("vulns", [])}
    return data


@pytest.fixture
def offline(monkeypatch):
    def unreachable(name, timeout=10):
        raise NetworkUnavailable("no network")
    monkeypatch.setattr(deps, "fetch_pypi", unreachable)


def write_source(ws: Path, source: str, name: str = "app.py") -> None:
    (ws / name).write_text(source, encoding="utf-8")


def test_a_stdlib_only_project_passes_without_any_network(tmp_path, offline):
    write_source(tmp_path, "import json\nimport re\nfrom collections import Counter\n")
    result = run_g8(tmp_path)
    assert result.status is Status.PASS
    assert "standard library only" in result.summary


def test_test_files_may_import_the_toolchain(tmp_path, offline):
    """pytest and hypothesis belong to the sandbox, not to the project."""
    write_source(tmp_path, "x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "import pytest\nfrom hypothesis import given\n", encoding="utf-8")
    assert run_g8(tmp_path).status is Status.PASS


def test_sibling_modules_are_not_third_party(tmp_path, offline):
    write_source(tmp_path, "def tokenize(t):\n    return []\n", "tokenizer.py")
    write_source(tmp_path, "from tokenizer import tokenize\n", "wordstats.py")
    assert run_g8(tmp_path).status is Status.PASS


def test_an_undeclared_import_fails_even_with_no_network(tmp_path, offline):
    """The offline tier alone catches a hallucinated dependency."""
    write_source(tmp_path, "import fastjsonx\n")
    result = run_g8(tmp_path)
    assert result.status is Status.FAIL
    (finding,) = result.findings
    assert finding.code == "undeclared-dependency"
    assert finding.file == "app.py" and finding.line == 1
    assert "fastjsonx" in finding.message


def test_a_declared_package_that_does_not_exist_is_critical(tmp_path, monkeypatch):
    write_source(tmp_path, "import fastjsonx\n")
    declare(tmp_path, "fastjsonx")

    def missing(name, timeout=10):
        raise PackageMissing(name)
    monkeypatch.setattr(deps, "fetch_pypi", missing)

    result = run_g8(tmp_path)
    assert result.status is Status.FAIL
    assert any(f.code == "package-does-not-exist"
               and f.severity is Severity.CRITICAL for f in result.findings)


def test_a_known_vulnerability_blocks(tmp_path, monkeypatch):
    write_source(tmp_path, "import oldlib\n")
    declare(tmp_path, "oldlib")
    monkeypatch.setattr(deps, "fetch_pypi", lambda n, timeout=10: pypi(
        vulns=[{"id": "PYSEC-2024-1"}]))
    result = run_g8(tmp_path)
    assert result.status is Status.FAIL
    assert any(f.code == "known-vulnerability" for f in result.findings)
    assert "PYSEC-2024-1" in result.findings[0].message


def test_a_declared_clean_package_passes(tmp_path, monkeypatch):
    write_source(tmp_path, "import requests\n")
    declare(tmp_path, "requests")
    monkeypatch.setattr(deps, "fetch_pypi", lambda n, timeout=10: pypi())
    assert run_g8(tmp_path).status is Status.PASS


def test_declared_but_unreachable_is_skipped_not_passed(tmp_path, offline):
    """Honesty: the online checks did not run, so nothing is claimed."""
    write_source(tmp_path, "import requests\n")
    declare(tmp_path, "requests")
    result = run_g8(tmp_path)
    assert result.status is Status.SKIP
    assert "unreachable" in (result.note or "")


def test_a_brand_new_package_is_flagged_but_does_not_block(tmp_path, monkeypatch):
    write_source(tmp_path, "import freshlib\n")
    declare(tmp_path, "freshlib")
    monkeypatch.setattr(deps, "fetch_pypi", lambda n, timeout=10: pypi(age=5))
    result = run_g8(tmp_path)
    assert result.status is Status.PASS
    assert any(f.code == "very-new-package" for f in result.findings)


def test_import_names_are_mapped_to_distribution_names(tmp_path, monkeypatch):
    write_source(tmp_path, "import yaml\n")
    declare(tmp_path, "PyYAML")
    asked = []
    monkeypatch.setattr(deps, "fetch_pypi",
                        lambda n, timeout=10: asked.append(n) or pypi())
    assert run_g8(tmp_path).status is Status.PASS
    assert asked == ["PyYAML"]


# -- multi-module conformance ---------------------------------------------------

def test_g7_checks_every_modules_spec(tmp_path: Path):
    (tmp_path / "tokenizer.py").write_text(
        "def tokenize(text):\n    return text.split()\n", encoding="utf-8")
    (tmp_path / "doubler.py").write_text(
        "def double(x):\n    return x * 2\n", encoding="utf-8")
    specs = tmp_path / ".codecrew" / "specs"
    specs.mkdir(parents=True)

    def spec(module, signature, call, result):
        return json.dumps({"summary": "s", "module": module, "functions": [{
            "signature": signature, "description": "d", "raises": [],
            "examples": [{"call": call, "result": result}]}]})

    (specs / "tokenizer.json").write_text(
        spec("tokenizer", "tokenize(text)", "tokenize('a b')", "['a', 'b']"),
        encoding="utf-8")
    (specs / "doubler.json").write_text(
        spec("doubler", "double(x)", "double(2)", "5"), encoding="utf-8")   # wrong

    report = GateRunner(tmp_path, GateConfig(sandbox="local")).run(only=["G7"])
    (g7,) = report.results
    assert g7.status is Status.FAIL
    assert "1/2" in g7.summary
    (finding,) = g7.findings
    assert finding.file == "doubler.py", "the finding names the module at fault"


# -- who owns a failure ---------------------------------------------------------

def failing_report(*files: str) -> GateReport:
    findings = [Finding(Severity.HIGH, "x", "m", f) for f in files]
    return GateReport("r", "w", "stub", [GateResult(
        gate_id="G2", name="t", status=Status.FAIL, summary="s",
        findings=findings, blocking=True)])


@pytest.mark.parametrize("file,expected", [
    ("tests/test_wordstats.py", "wordstats"),
    ("wordstats.py", "wordstats"),
    ("C:\\proj\\ws\\tokenizer.py", "tokenizer"),
    ("tests/test_tokenizer.py", "tokenizer"),
])
def test_the_owner_is_the_module_a_finding_points_at(file, expected):
    assert owner_module(failing_report(file), ["tokenizer", "wordstats"]) == expected


def test_with_no_pointer_the_first_built_module_owns_it():
    assert owner_module(failing_report(), ["tokenizer", "wordstats"]) == "tokenizer"
    assert owner_module(failing_report("unrelated.py"),
                        ["tokenizer", "wordstats"]) == "tokenizer"


# -- the project pipeline -------------------------------------------------------

TOKEN_MARK, STATS_MARK = "TOKENIZER-IMPL-MARK-31", "WORDSTATS-IMPL-MARK-77"
TOKEN_TEST_MARK, STATS_TEST_MARK = "TOKENIZER-TEST-MARK-52", "WORDSTATS-TEST-MARK-90"


def spec_dict(module, signature, call, result):
    return {"summary": f"{module}.", "module": module,
            "functions": [{"signature": signature, "description": "d",
                           "raises": [],
                           "examples": [{"call": call, "result": result}]}],
            "security": ["none"], "assumptions": []}


SPEC_TOKEN = spec_dict("tokenizer", "tokenize(text: str) -> list[str]",
                       "tokenize('A b')", "['a', 'b']")
SPEC_STATS = spec_dict("wordstats", "summarize(text: str, top: int = 3) -> dict",
                       "summarize('a a')",
                       "{'total': 2, 'unique': 1, 'longest': 'a', 'top': [('a', 2)]}")

REF = PROJECT_TASKS[0].reference_files


def files_json(path: str, content: str) -> str:
    return json.dumps({"files": [{"path": path, "content": content}]})


def impl(name: str, mark: str) -> str:
    return files_json(f"{name}.py", f"# {mark}\n" + REF[f"{name}.py"].lstrip("\n"))


TOKEN_TESTS = (f"# {TOKEN_TEST_MARK}\nfrom tokenizer import tokenize\n\n"
               "def test_basic():\n    assert tokenize('A b') == ['a', 'b']\n")
STATS_TESTS = (f"# {STATS_TEST_MARK}\nfrom wordstats import summarize\n\n"
               "def test_basic():\n"
               "    assert summarize('a a') == {'total': 2, 'unique': 1, "
               "'longest': 'a', 'top': [('a', 2)]}\n")


def project_script(stats_spec=None) -> list[str]:
    """Responses in the order the pipeline will ask for them."""
    return [
        json.dumps(PLAN),                                        # architect
        json.dumps(SPEC_TOKEN),                                  # tokenizer
        impl("tokenizer", TOKEN_MARK),
        files_json("tests/test_tokenizer.py", TOKEN_TESTS),
        json.dumps(stats_spec or SPEC_STATS),                    # wordstats
        impl("wordstats", STATS_MARK),
        files_json("tests/test_wordstats.py", STATS_TESTS),
    ]


@pytest.fixture
def scripted(monkeypatch):
    def factory(responses: list[str]) -> ScriptedProvider:
        provider = ScriptedProvider(responses)
        monkeypatch.setattr("codecrew.llm.client.select_provider",
                            lambda name="auto", script=None: provider)
        return provider
    return factory


def project_pipeline(gates: GateConfig | None = None) -> Pipeline:
    return Pipeline(LLMConfig(provider="scripted", backoff_base_s=0.0),
                    gates or GateConfig(sandbox="local"))


def flat(messages) -> str:
    return "\n".join(m.content for m in messages)


def test_modules_are_built_dependencies_first(scripted, tmp_path: Path):
    provider = scripted(project_script())
    result = project_pipeline().build("text stats", tmp_path / "ws",
                                      mode="project", verify=False)
    assert result.failure is None, result.failure
    assert list(result.specs) == ["tokenizer", "wordstats"]
    # call 1 is the first spec request: it must be the tokenizer's
    assert "named `tokenizer`" in flat(provider.calls[1])
    assert "named `wordstats`" in flat(provider.calls[4])


def test_a_module_is_handed_the_signatures_of_what_it_may_import(
    scripted, tmp_path: Path
):
    provider = scripted(project_script())
    result = project_pipeline().build("text stats", tmp_path / "ws",
                                      mode="project", verify=False)

    signature = "tokenizer.tokenize(text: str) -> list[str]"
    assert signature in flat(provider.calls[4]), "the spec agent is told"
    assert signature in flat(provider.calls[5]), "and so is the implementer"
    # Set by the pipeline from the built spec, so the model cannot drift it.
    assert result.specs["wordstats"].uses == [signature]
    assert result.specs["tokenizer"].uses == []


def test_isolation_holds_across_modules(scripted, tmp_path: Path):
    provider = scripted(project_script())
    project_pipeline().build("text stats", tmp_path / "ws", mode="project",
                             verify=False)
    tok_tests, stats_impl, stats_tests = (
        flat(provider.calls[3]), flat(provider.calls[5]), flat(provider.calls[6]))

    assert TOKEN_MARK not in tok_tests            # test author never sees code
    assert STATS_MARK not in stats_tests
    assert TOKEN_TEST_MARK not in stats_impl      # nor does an implementer
    assert STATS_TEST_MARK not in stats_impl
    assert "text stats" not in stats_impl         # nor the raw requirement


def test_plan_specs_and_requirements_are_saved(scripted, tmp_path: Path):
    plan = json.loads(json.dumps(PLAN))
    plan["dependencies"] = [{"name": "requests", "purpose": "http"}]
    script = project_script()
    script[0] = json.dumps(plan)
    scripted(script)
    project_pipeline().build("text stats", tmp_path / "ws", mode="project",
                             verify=False)

    work = tmp_path / "ws" / ".codecrew"
    assert json.loads((work / "plan.json").read_text("utf-8"))["summary"]
    assert {p.name for p in (work / "specs").glob("*.json")} == {
        "tokenizer.json", "wordstats.json"}
    assert (tmp_path / "ws" / "requirements.txt").read_text("utf-8") == "requests\n"
    assert (tmp_path / "ws" / "tokenizer.py").exists()
    assert (tmp_path / "ws" / "tests" / "test_wordstats.py").exists()


def test_no_requirements_file_for_a_stdlib_only_plan(scripted, tmp_path: Path):
    scripted(project_script())
    project_pipeline().build("text stats", tmp_path / "ws", mode="project",
                             verify=False)
    assert not (tmp_path / "ws" / "requirements.txt").exists()


def test_a_rejected_plan_stops_before_any_spec_is_written(scripted, tmp_path: Path):
    provider = scripted(project_script())
    result = project_pipeline().build(
        "text stats", tmp_path / "ws", mode="project",
        approve_plan=lambda plan: None, verify=False)
    assert result.failure and "plan was rejected" in result.failure
    assert len(provider.calls) == 1, "only the architect ran"


def test_a_rejected_module_spec_stops_the_build(scripted, tmp_path: Path):
    provider = scripted(project_script())
    result = project_pipeline().build(
        "text stats", tmp_path / "ws", mode="project",
        approve=lambda spec: None, verify=False)
    assert result.failure and "'tokenizer'" in result.failure
    assert len(provider.calls) == 2, "architect and the first spec only"


def test_the_planned_module_name_wins_over_the_spec_agents(scripted, tmp_path: Path):
    """Other modules already import it by the planned name."""
    renamed = json.loads(json.dumps(SPEC_TOKEN))
    renamed["module"] = "splitter"
    script = project_script()
    script[1] = json.dumps(renamed)
    scripted(script)
    result = project_pipeline().build("text stats", tmp_path / "ws",
                                      mode="project", verify=False)
    assert result.failure is None
    assert "tokenizer" in result.specs and "splitter" not in result.specs


# -- the benchmark's project task -----------------------------------------------

def test_project_tasks_stay_out_of_the_default_suite():
    assert all(t.tier != "project" for t in TASKS)
    assert [t.id for t in get_tasks()] == [t.id for t in TASKS]
    assert get_tasks(["wordstats"])[0].tier == "project"


def test_the_project_reference_passes_its_hidden_tests(tmp_path: Path):
    import subprocess
    import sys

    task = PROJECT_TASKS[0]
    for path, source in task.reference_sources().items():
        (tmp_path / path).write_text(source.lstrip("\n"), encoding="utf-8")
    (tmp_path / "test_hidden.py").write_text(task.hidden_tests.lstrip("\n"),
                                             encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "test_hidden.py", "-q",
         "-p", "no:cacheprovider", "--no-header"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_project_requirement_pins_both_modules():
    task = PROJECT_TASKS[0]
    for name in ("tokenizer", "wordstats"):
        assert f"`{name}.py`" in task.requirement
        assert f"from {name} import" in task.hidden_tests


@pytest.mark.slow
def test_gates_never_reject_the_project_reference(tmp_path: Path):
    task = PROJECT_TASKS[0]
    for path, source in task.reference_sources().items():
        (tmp_path / path).write_text(source.lstrip("\n"), encoding="utf-8")
    report = GateRunner(tmp_path, GateConfig(sandbox="local")).run(
        only=["G1", "G5", "G8"])
    assert [r.gate_id for r in report.results if r.blocked] == []


# -- end to end through the real gates ------------------------------------------

@pytest.mark.slow
def test_a_project_goes_through_every_gate(scripted, tmp_path: Path):
    scripted(project_script())
    config = GateConfig(sandbox="local", disabled=("G3", "G4", "G6"))
    result = project_pipeline(config).build("text stats", tmp_path / "ws",
                                            mode="project")
    assert result.report is not None
    by_id = {r.gate_id: r for r in result.report.results}
    assert by_id["G1"].status is Status.PASS
    assert by_id["G8"].status is Status.PASS
    assert by_id["G7"].status is Status.PASS
    assert "2/2" in by_id["G7"].summary, "both modules' specs were executed"
    assert by_id["G2"].status is Status.PASS
    assert result.passed


@pytest.mark.slow
def test_a_wrong_spec_example_in_one_module_is_repaired_in_that_module(
    scripted, tmp_path: Path
):
    """The loop must arbitrate against the right module's spec, write the
    correction to that module's file, and leave the others untouched."""
    wrong = json.loads(json.dumps(SPEC_STATS))
    wrong["functions"][0]["examples"][0]["result"] = (
        "{'total': 3, 'unique': 1, 'longest': 'a', 'top': [('a', 3)]}")

    diagnosis = json.dumps({
        "verdict": "spec_defect", "target_files": [],
        "root_cause": "the example counts three words in 'a a'",
        "evidence": "'a a' has two words",
        "fix_instruction": "change total to 2 and the count to 2"})
    script = project_script(stats_spec=wrong) + [
        diagnosis,
        json.dumps(SPEC_STATS),
        files_json("tests/test_wordstats.py", STATS_TESTS),
    ]
    scripted(script)
    config = GateConfig(sandbox="local", disabled=("G3", "G4", "G6"))
    result = project_pipeline(config).build(
        "text stats", tmp_path / "ws", mode="project", repair_rounds=2)

    assert result.initial_verdict == "UNVERIFIED"
    assert [(s.verdict, s.outcome) for s in result.repairs] == [
        ("spec_defect", "accepted")]
    assert result.passed

    specs = tmp_path / "ws" / ".codecrew" / "specs"
    fixed = json.loads((specs / "wordstats.json").read_text("utf-8"))
    assert "'total': 2" in fixed["functions"][0]["examples"][0]["result"]
    assert (tmp_path / "ws" / "tokenizer.py").read_text("utf-8") == \
        "# {}\n".format(TOKEN_MARK) + REF["tokenizer.py"].lstrip("\n")
