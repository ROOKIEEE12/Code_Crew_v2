"""Projects the UI tracks, and what it shows about each.

A project is a directory on disk. Its history is the reports CodeCrew has
already written into ``<project>/.codecrew/reports/``, so there is no
separate database to drift out of sync, and a run made from the command
line shows up in the UI too.

Which directories the server may touch is decided here: the workspace
roots it was started with, plus the projects the person registered
explicitly (``~/.codecrew/projects.json``). Registering is a deliberate act
from the UI or the CLI; the server's Origin and Host checks keep other web
pages from doing it.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from ..badges import Review, compute_badges
from ..config import WORK_DIR_NAME
from ..results import GateReport

MAX_HISTORY = 30
EXCERPT_CONTEXT = 2
MAX_FINDINGS = 300
MAX_FILE_BYTES = 400_000


class ProjectError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def default_registry_path() -> Path:
    override = os.environ.get("CODECREW_HOME")
    base = Path(override) if override else Path.home() / ".codecrew"
    return base / "projects.json"


class ProjectRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_registry_path()
        self._lock = threading.Lock()

    def paths(self) -> list[Path]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out = []
        for raw in data.get("projects", []) if isinstance(data, dict) else []:
            if isinstance(raw, str):
                out.append(Path(raw))
        return out

    def add(self, raw: str) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ProjectError("give the project's full path")
        path = path.resolve()
        if not path.is_dir():
            raise ProjectError(f"not a directory: {path}", 404)
        if path == Path(path.anchor) or path == Path.home().resolve():
            raise ProjectError("that is a whole drive or your home folder; "
                               "choose the project's own folder")
        with self._lock:
            projects = [p for p in self.paths() if p != path]
            projects.insert(0, path)
            self._write(projects)
        return path

    def remove(self, raw: str) -> None:
        path = Path(raw).expanduser().resolve()
        with self._lock:
            self._write([p for p in self.paths() if p != path])

    def _write(self, projects: list[Path]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"projects": [str(p) for p in projects]}, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)


# -- reading a project ---------------------------------------------------------------

def report_files(path: Path) -> list[Path]:
    reports = path / WORK_DIR_NAME / "reports"
    if not reports.is_dir():
        return []
    return sorted(reports.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def load_report(file: Path) -> dict | None:
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "results" in data else None


def run_summary(file: Path, data: dict) -> dict:
    by_id = {r.get("gate_id"): r for r in data.get("results", [])}

    def score(gate_id: str):
        return (by_id.get(gate_id) or {}).get("score")

    disabled = [r["gate_id"] for r in data.get("results", [])
                if r.get("note") == "disabled in configuration"]
    return {
        "run_id": data.get("run_id", file.stem),
        "verdict": data.get("verdict"),
        "passed": data.get("passed"),
        "time": file.stat().st_mtime,
        "duration_s": round(data.get("duration_s") or 0.0, 1),
        "counts": data.get("counts", {}),
        "coverage": score("G3"),
        "mutation": score("G4"),
        "tests": (by_id.get("G2") or {}).get("score"),
        "profile": "quick" if "G4" in disabled else "full",
        "gates": {r.get("gate_id"): r.get("status") for r in data.get("results", [])},
    }


def summarize_project(path: Path, *, registered: bool) -> dict:
    files = report_files(path)
    history = []
    for file in files[:MAX_HISTORY]:
        data = load_report(file)
        if data is not None:
            history.append(run_summary(file, data))
    generated = (path / WORK_DIR_NAME / "spec.json").exists() or \
        (path / WORK_DIR_NAME / "plan.json").exists()
    latest = history[0] if history else None
    return {
        "name": path.name,
        "path": str(path),
        "registered": registered,
        "kind": "generated" if generated else "project",
        "exists": path.is_dir(),
        "latest": latest,
        "history": list(reversed(history[:12])),  # oldest first, for trends
        "runs": len(files),
    }


def project_detail(path: Path, run_id: str | None = None, *,
                   registered: bool = True) -> dict:
    from ..project import Project, ProjectConfigError

    if not path.is_dir():
        raise ProjectError("that project folder no longer exists", 404)
    files = report_files(path)
    history = []
    chosen: dict | None = None
    for file in files[:MAX_HISTORY]:
        data = load_report(file)
        if data is None:
            continue
        summary = run_summary(file, data)
        history.append(summary)
        if chosen is None and (run_id is None or summary["run_id"] == run_id):
            chosen = data
    if run_id is not None and chosen is None:
        raise ProjectError(f"no run {run_id!r} in this project", 404)

    info: dict = {"config_error": None}
    try:
        project = Project.detect(path)
        info.update(
            source_root=project.source_root,
            settings=project.settings,
            settings_file=project.settings_file,
            dependencies=len(project.dependencies),
            requirement_files=project.requirement_files,
            generated=project.generated,
            install_dependencies=project.should_install(),
            warnings=project.warnings,
        )
    except ProjectConfigError as exc:
        info["config_error"] = str(exc)

    report = badges = None
    findings: list[dict] = []
    if chosen is not None:
        gate_report = GateReport.from_dict(chosen)
        badges = [b.__dict__ for b in compute_badges(gate_report, path)]
        report = chosen
        findings = findings_with_excerpts(path, gate_report)
    review = Review.load(path)
    return {
        **summarize_project(path, registered=registered),
        "info": info,
        "report": report,
        "badges": badges or [],
        "findings": findings,
        "history": history,
        "verdict_note": review.caveat() if review and report else None,
    }


def findings_with_excerpts(root: Path, report: GateReport) -> list[dict]:
    """Every finding, with the lines around it when it has a location."""
    cache: dict[str, list[str] | None] = {}
    out = []
    for gate in report.results:
        for finding in gate.findings:
            if len(out) >= MAX_FINDINGS:
                return out
            rel = relative_path(root, finding.file)
            excerpt = None
            if rel and finding.line:
                lines = cache.get(rel)
                if rel not in cache:
                    lines = _read_lines(root / rel)
                    cache[rel] = lines
                if lines:
                    start = max(1, finding.line - EXCERPT_CONTEXT)
                    end = min(len(lines), finding.line + EXCERPT_CONTEXT)
                    excerpt = {"start": start, "line": finding.line,
                               "lines": lines[start - 1:end]}
            out.append({
                "gate": gate.gate_id, "gate_name": gate.name,
                "gate_status": gate.status.value, "blocking": gate.blocking,
                "severity": finding.severity.value, "code": finding.code,
                "message": finding.message, "file": rel or finding.file,
                "line": finding.line, "excerpt": excerpt,
            })
    return out


def relative_path(root: Path, raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.replace("\\", "/")
    candidate = Path(text)
    try:
        if candidate.is_absolute():
            return candidate.resolve().relative_to(root.resolve()).as_posix()
        resolved = (root / text).resolve()
        resolved.relative_to(root.resolve())
        return Path(text).as_posix().removeprefix("./")
    except ValueError:
        return None


def read_file(root: Path, rel: str) -> dict:
    """A file inside a project, for the code viewer. Never outside it."""
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        raise ProjectError("that file is outside the project", 403) from None
    if not target.is_file():
        raise ProjectError("no such file", 404)
    if target.stat().st_size > MAX_FILE_BYTES:
        raise ProjectError("file too large to display", 413)
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ProjectError("not a text file", 415) from None
    return {"path": rel, "content": text, "modified": target.stat().st_mtime}


def _read_lines(path: Path) -> list[str] | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
