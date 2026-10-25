"""Projects in the local API: registering a repository, its run history, and
reading its files, all without stepping outside what the person allowed."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from codecrew.results import Finding, GateReport, GateResult, Severity, Status
from codecrew.server import JobManager, create_server
from codecrew.server.projects import (
    ProjectError,
    ProjectRegistry,
    findings_with_excerpts,
    project_detail,
    relative_path,
)


def write_report(project: Path, run_id: str, *, status=Status.PASS, coverage=90.0,
                 findings=()):
    report = GateReport(run_id, str(project), "local-venv", [
        GateResult("G2", "Test execution", Status.PASS, "3 passed", score=3.0),
        GateResult("G3", "Branch coverage", status, "cov", score=coverage,
                   findings=list(findings)),
    ], sources=["app.py"])
    report.write_json(project / ".codecrew" / "reports" / f"{run_id}.json")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "myapp"
    root.mkdir()
    (root / "app.py").write_text("a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n", encoding="utf-8")
    return root


@pytest.fixture
def manager(tmp_path: Path):
    m = JobManager([tmp_path / "ws"], registry=ProjectRegistry(tmp_path / "reg.json"),
                   runner=lambda job, mgr: {"verdict": "VERIFIED"})
    yield m
    m.shutdown()


def test_registering_a_project_makes_it_an_allowed_path(manager, project):
    with pytest.raises(Exception) as before:
        manager.inside_roots(str(project))
    assert before.value.status == 403
    added = manager.add_project(str(project))
    assert added["name"] == "myapp" and added["registered"]
    assert manager.inside_roots(str(project / "app.py")) == (project / "app.py").resolve()
    manager.remove_project(str(project))
    assert manager.registry.paths() == []


@pytest.mark.parametrize("bad", ["relative/path", "/definitely/not/here"])
def test_bad_registrations_are_explained(manager, bad):
    with pytest.raises(Exception) as info:
        manager.add_project(bad)
    assert info.value.status in (400, 404)


def test_the_home_folder_is_too_broad_to_register(tmp_path):
    with pytest.raises(ProjectError, match="home"):
        ProjectRegistry(tmp_path / "r.json").add(str(Path.home()))


def test_history_comes_from_the_reports_on_disk(project):
    import os
    import time
    write_report(project, "run-1", coverage=60.0, status=Status.FAIL)
    time.sleep(0.02)
    write_report(project, "run-2", coverage=85.0)
    os.utime(project / ".codecrew" / "reports" / "run-2.json")
    detail = project_detail(project)
    assert [h["run_id"] for h in detail["history"]] == ["run-2", "run-1"]
    assert detail["latest"]["coverage"] == 85.0
    assert detail["report"]["run_id"] == "run-2"
    older = project_detail(project, "run-1")
    assert older["report"]["run_id"] == "run-1"
    assert older["badges"][0]["badge"] == "UNVERIFIED"
    with pytest.raises(ProjectError):
        project_detail(project, "nope")


def test_findings_carry_the_code_around_them(project):
    report = GateReport("r", str(project), "s", [GateResult(
        "G4", "Mutation", Status.PASS, "s",
        findings=[Finding(Severity.MEDIUM, "survived", "x", file="app.py", line=3),
                  Finding(Severity.LOW, "note", "no location")])])
    first, second = findings_with_excerpts(project, report)
    assert first["excerpt"] == {"start": 1, "line": 3,
                                "lines": ["a = 1", "b = 2", "c = 3", "d = 4", "e = 5"]}
    assert second["excerpt"] is None


def test_paths_outside_the_project_are_never_read(project):
    assert relative_path(project, "../secret.txt") is None
    assert relative_path(project, str(project / "app.py")) == "app.py"
    from codecrew.server.projects import read_file
    with pytest.raises(ProjectError) as info:
        read_file(project, "../../etc/passwd")
    assert info.value.status == 403


def test_projects_api_end_to_end(manager, project):
    srv = create_server(port=0, manager=manager)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import http.client

    def call(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        headers = {"Host": f"127.0.0.1:{srv.server_address[1]}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    try:
        write_report(project, "run-1")
        status, _ = call("POST", "/api/projects", {"path": str(project)})
        assert status == 201
        status, data = call("GET", "/api/projects")
        assert data["projects"][0]["latest"]["verdict"] == "VERIFIED"
        from urllib.parse import quote
        status, data = call("GET", f"/api/project?path={quote(str(project))}")
        assert status == 200 and data["badges"][0]["path"] == "app.py"
        status, data = call("GET", f"/api/project/file?path={quote(str(project))}&file=app.py")
        assert data["content"].startswith("a = 1")
        status, data = call("GET", f"/api/project/file?path={quote(str(project))}"
                                   f"&file=..%2F..%2Fsecret")
        assert status == 403
        status, data = call("GET", "/api/health")
        assert "tools" in data and "python" in data
        status, _ = call("POST", "/api/projects/remove", {"path": str(project)})
        assert status == 200
        status, data = call("GET", f"/api/project?path={quote(str(project))}")
        assert status == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_report_round_trips_through_json(project):
    write_report(project, "r1", findings=[Finding(Severity.HIGH, "x", "y", "app.py", 2)])
    data = json.loads((project / ".codecrew" / "reports" / "r1.json").read_text())
    rebuilt = GateReport.from_dict(data)
    assert rebuilt.results[1].findings[0].severity is Severity.HIGH
    assert rebuilt.sources == ["app.py"]
    assert rebuilt.verdict() == data["verdict"]
