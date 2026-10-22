"""Background jobs for the local API: build, verify, repair.

One worker runs one job at a time. That is deliberate: every job shares the
free-tier model quota and the sandbox's tool environment, and two builds
competing for either would only make both slower and the traces harder to
read. Further jobs wait in the queue and say so.

The approval checkpoint becomes a pause: the pipeline's approver blocks the
worker until a person answers through the API (or the wait times out, which
counts as a rejection, never as an approval).
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..agents import ContractError, ProjectPlan, Spec
from ..badges import Review, compute_badges, source_files
from ..config import WORK_DIR_NAME, GateConfig
from ..docs import USAGE_FILE
from ..gates import GateRunner
from ..llm import LLMConfig
from ..sandbox import get_sandbox

if TYPE_CHECKING:
    from .projects import ProjectRegistry

MODES = ("single", "multi", "project")
PROVIDERS = ("auto", "groq", "gemini", "ollama", "anthropic", "scripted")
MAX_REQUIREMENT = 8_000
MAX_EVENTS = 3_000
MAX_FILE_CHARS = 200_000
_STRING_CAP = 2_000


class JobError(ValueError):
    """A request the API should answer with 400 (or 404 / 409)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Job:
    id: str
    kind: str
    params: dict
    workspace: str = ""
    status: str = "queued"  # queued|running|awaiting_approval|done|failed|cancelled
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    events: list[dict] = field(default_factory=list)
    pending: dict | None = None
    result: dict | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._decided = threading.Event()
        self._decision: Any = None

    # -- events ---------------------------------------------------------------

    def add_event(self, event: dict) -> None:
        with self._lock:
            if len(self.events) >= MAX_EVENTS:
                return
            self.events.append(_cap(event))

    def summary(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "workspace": self.workspace, "created": self.created,
            "started": self.started, "finished": self.finished,
            "title": _title(self),
            "verdict": (self.result or {}).get("verdict"),
            "error": self.error,
        }

    def detail(self, since: int = 0) -> dict:
        with self._lock:
            events = self.events[since:]
            total = len(self.events)
        return {**self.summary(), "params": self.params, "pending": self.pending,
                "result": self.result, "events": events, "next": total}

    # -- approval -------------------------------------------------------------

    def request_approval(self, kind: str, payload: dict, timeout: float,
                         module: str | None = None):
        """Block until a person answers; ``None`` means rejected."""
        self._decided.clear()
        self._decision = None
        self.pending = {"type": kind, "payload": payload, "module": module,
                        "requested": time.time()}
        self.status = "awaiting_approval"
        self.add_event({"kind": "approval_requested", "type": kind,
                        "module": module})
        answered = self._decided.wait(timeout)
        self.pending = None
        self.status = "running"
        if not answered:
            self.add_event({"kind": "approval_timeout", "type": kind})
            return None
        self.add_event({"kind": "approval_decided", "type": kind,
                        "approved": self._decision is not None})
        return self._decision

    def decide(self, approve: bool, edited: dict | None) -> None:
        if self.pending is None:
            raise JobError("this job is not waiting for an approval", 409)
        kind = self.pending["type"]
        decision = None
        if approve:
            payload = edited if edited is not None else self.pending["payload"]
            try:
                decision = (Spec.from_dict(payload) if kind == "spec"
                            else ProjectPlan.from_dict(payload))
            except ContractError as exc:
                # The edit is refused and the job keeps waiting, so the person
                # can correct it rather than lose the run.
                raise JobError(f"the edited {kind} is invalid: {exc.message}")
            except (TypeError, AttributeError) as exc:
                raise JobError(f"the edited {kind} is malformed: {exc}")
        self._decision = decision
        self._decided.set()


Runner = Callable[[Job, "JobManager"], dict]


class JobManager:
    def __init__(
        self,
        roots: list[Path],
        *,
        runner: Runner | None = None,
        approval_timeout: float = 3600.0,
        gate_config: GateConfig | None = None,
        replays_dir: Path | None = None,
        eval_dir: Path | None = None,
        registry: "ProjectRegistry | None" = None,
    ) -> None:
        from .projects import ProjectRegistry

        #: Where `codecrew eval` writes its studies; read-only here.
        self.eval_dir = eval_dir
        #: Projects the person registered; they are allowed paths too.
        self.registry = registry or ProjectRegistry()
        if not roots:
            raise ValueError("at least one workspace root is required")
        self.roots = [root.resolve() for root in roots]
        for root in self.roots:
            root.mkdir(parents=True, exist_ok=True)
        self.approval_timeout = approval_timeout
        self.gate_config = gate_config
        self.replays_dir = replays_dir
        self._runner = runner or run_job
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._queue: queue.Queue[Job | None] = queue.Queue()
        self._worker = threading.Thread(target=self._work, daemon=True,
                                        name="codecrew-jobs")
        self._worker.start()

    # -- public API -------------------------------------------------------------

    def submit(self, params: dict) -> Job:
        kind = params.get("kind")
        if kind == "build":
            clean = _validate_build(params, self)
        elif kind in ("verify", "repair"):
            clean = _validate_existing(params, self)
        else:
            raise JobError("kind must be 'build', 'verify' or 'repair'")
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, params=clean)
        job.workspace = str(clean.get("workspace", ""))
        self._jobs[job.id] = job
        self._order.append(job.id)
        self._queue.put(job)
        job.add_event({"kind": "queued", "position": self._queue.qsize()})
        return job

    def get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobError(f"no job {job_id!r}", 404)
        return job

    def list_jobs(self) -> list[dict]:
        return [self._jobs[i].summary() for i in reversed(self._order)]

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status == "queued":
            job.status = "cancelled"
            job.finished = time.time()
        elif job.status == "awaiting_approval":
            job.decide(False, None)  # a rejection stops the pipeline cleanly
        else:
            raise JobError(f"a {job.status} job cannot be cancelled", 409)
        return job

    def inside_roots(self, raw: str) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.roots[0] / path
        path = path.resolve()
        for root in [*self.roots, *self.registry.paths()]:
            root = root.resolve()
            if path == root or root in path.parents:
                return path
        raise JobError("that folder is not a registered project and not inside "
                       "the workspace roots; add it as a project first", 403)

    # -- projects -------------------------------------------------------------------

    def projects(self) -> list[dict]:
        """Registered projects first, then generated builds in the roots."""
        from .projects import summarize_project

        seen: set[Path] = set()
        out = []
        for path in self.registry.paths():
            seen.add(path.resolve())
            out.append(summarize_project(path, registered=True))
        builds = []
        for root in self.roots:
            for child in root.iterdir():
                if child.is_dir() and (child / WORK_DIR_NAME).is_dir() \
                        and child.resolve() not in seen:
                    builds.append(child)
        builds.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        out += [summarize_project(p, registered=False) for p in builds[:100]]
        return out

    def add_project(self, raw: str) -> dict:
        from .projects import ProjectError, summarize_project

        try:
            path = self.registry.add(raw)
        except ProjectError as exc:
            raise JobError(str(exc), exc.status) from exc
        return summarize_project(path, registered=True)

    def remove_project(self, raw: str) -> None:
        self.registry.remove(raw)

    def project_detail(self, raw: str, run_id: str | None = None) -> dict:
        from .projects import ProjectError, project_detail

        path = self.inside_roots(raw)
        registered = path in [p.resolve() for p in self.registry.paths()]
        try:
            return project_detail(path, run_id, registered=registered)
        except ProjectError as exc:
            raise JobError(str(exc), exc.status) from exc

    def project_file(self, raw: str, rel: str) -> dict:
        from .projects import ProjectError, read_file

        path = self.inside_roots(raw)
        try:
            return read_file(path, rel)
        except ProjectError as exc:
            raise JobError(str(exc), exc.status) from exc

    def workspaces(self) -> list[dict]:
        """Previous builds on disk, newest first."""
        found = []
        for root in self.roots:
            for child in root.iterdir():
                if child.is_dir() and (child / WORK_DIR_NAME).is_dir():
                    found.append(child)
        found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for path in found[:100]:
            report = _latest_report(path)
            out.append({"name": path.name, "path": str(path),
                        "modified": path.stat().st_mtime,
                        "verdict": report.get("verdict") if report else None})
        return out

    def workspace_detail(self, raw: str) -> dict:
        path = self.inside_roots(raw)
        if not path.is_dir():
            raise JobError("no such workspace", 404)
        return describe_workspace(path)

    # -- worker -------------------------------------------------------------------

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            if job.status == "cancelled":
                continue
            job.status = "running"
            job.started = time.time()
            job.add_event({"kind": "job_start"})
            try:
                job.result = self._runner(job, self)
                job.status = "done"
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                job.error = f"{type(exc).__name__}: {exc}"
                job.status = "failed"
            finally:
                job.pending = None
                job.finished = time.time()
                job.add_event({"kind": "job_end", "status": job.status})

    def shutdown(self) -> None:
        self._queue.put(None)


# -- validation --------------------------------------------------------------------

def _validate_build(params: dict, manager: JobManager) -> dict:
    requirement = params.get("requirement")
    if not isinstance(requirement, str) or not requirement.strip():
        raise JobError("'requirement' must be a non-empty string")
    if len(requirement) > MAX_REQUIREMENT:
        raise JobError(f"'requirement' is longer than {MAX_REQUIREMENT} characters")
    mode = params.get("mode", "multi")
    if mode not in MODES:
        raise JobError(f"'mode' must be one of {', '.join(MODES)}")
    provider = params.get("provider", "auto")
    if provider not in PROVIDERS:
        raise JobError(f"'provider' must be one of {', '.join(PROVIDERS)}")
    repair = params.get("repair", 2)
    if not isinstance(repair, int) or isinstance(repair, bool) or not 0 <= repair <= 4:
        raise JobError("'repair' must be an integer from 0 to 4")
    approval = params.get("approval", "review")
    if approval not in ("review", "auto"):
        raise JobError("'approval' must be 'review' or 'auto'")

    script = None
    if provider == "scripted":
        script = _replay_path(params.get("script"), manager)

    slug = re.sub(r"[^a-z0-9]+", "-", requirement.lower())[:32].strip("-") or "build"
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}"
    workspace = manager.roots[0] / name
    return {"requirement": requirement.strip(), "mode": mode,
            "provider": provider, "repair": repair, "approval": approval,
            "script": script, "workspace": str(workspace)}


def _validate_existing(params: dict, manager: JobManager) -> dict:
    raw = params.get("workspace")
    if not isinstance(raw, str) or not raw.strip():
        raise JobError("'workspace' must be a path")
    path = manager.inside_roots(raw.strip())
    if not path.is_dir():
        raise JobError(f"not a directory: {path}", 404)
    clean: dict[str, Any] = {"workspace": str(path)}
    if params["kind"] == "verify":
        profile = params.get("profile", "full")
        if profile not in ("full", "quick"):
            raise JobError("'profile' must be 'full' or 'quick'")
        clean["profile"] = profile
    if params["kind"] == "repair":
        rounds = params.get("rounds", 2)
        if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 4:
            raise JobError("'rounds' must be an integer from 1 to 4")
        provider = params.get("provider", "auto")
        if provider not in PROVIDERS or provider == "scripted":
            raise JobError("repair needs a live provider")
        clean.update(rounds=rounds, provider=provider)
    return clean


def _replay_path(raw: Any, manager: JobManager) -> str:
    """Scripted runs may only replay files from the bundled replays folder."""
    if manager.replays_dir is None:
        raise JobError("no replay scripts are available on this server")
    if not isinstance(raw, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", raw):
        raise JobError("'script' must be the name of a bundled replay, "
                       "e.g. 'clamp_multi.json'")
    path = manager.replays_dir / raw
    if not path.is_file():
        raise JobError(f"no bundled replay named {raw!r}", 404)
    return str(path)


def available_replays(replays_dir: Path | None) -> list[str]:
    if replays_dir is None or not replays_dir.is_dir():
        return []
    return sorted(p.name for p in replays_dir.glob("*.json"))


# -- running -------------------------------------------------------------------------

def run_job(job: Job, manager: JobManager) -> dict:
    if job.kind == "build":
        return _run_build(job, manager)
    if job.kind == "verify":
        return _run_verify(job, manager)
    return _run_repair(job, manager)


def _gate_config(manager: JobManager) -> GateConfig:
    return manager.gate_config or GateConfig()


def _run_build(job: Job, manager: JobManager) -> dict:
    from ..pipeline import Pipeline

    p = job.params
    pipeline = Pipeline(
        LLMConfig(provider=p["provider"], script=p["script"]),
        _gate_config(manager), on_event=job.add_event)

    approve = approve_plan = None
    if p["approval"] == "review" and p["mode"] != "single":
        def approve(spec: Spec):
            return job.request_approval("spec", spec.to_dict(),
                                        manager.approval_timeout, module=spec.module)

        def approve_plan(plan: ProjectPlan):
            return job.request_approval("plan", plan.to_dict(),
                                        manager.approval_timeout)

    result = pipeline.build(
        p["requirement"], Path(p["workspace"]), mode=p["mode"],
        approve=approve, approve_plan=approve_plan if p["mode"] == "project" else None,
        repair_rounds=p["repair"] if p["mode"] != "single" else 0)
    return serialize_run(result)


def _run_repair(job: Job, manager: JobManager) -> dict:
    from ..pipeline import Pipeline

    p = job.params
    pipeline = Pipeline(LLMConfig(provider=p["provider"]), _gate_config(manager),
                        on_event=job.add_event)
    try:
        result = pipeline.repair(Path(p["workspace"]), rounds=p["rounds"])
    except FileNotFoundError as exc:
        raise JobError(str(exc)) from exc
    return serialize_run(result)


def _run_verify(job: Job, manager: JobManager) -> dict:
    import copy

    from ..project import PROFILES, Project, ProjectConfigError

    workspace = Path(job.params["workspace"])
    config = copy.deepcopy(_gate_config(manager))
    try:
        project = Project.detect(workspace)
    except ProjectConfigError as exc:
        raise JobError(f"[tool.codecrew] is invalid: {exc}") from exc
    project.apply(config)
    profile = job.params.get("profile")
    if profile:
        config.disabled = tuple(dict.fromkeys([*config.disabled, *PROFILES[profile]]))
    sandbox = get_sandbox(workspace, config.sandbox, project=project)
    runner = GateRunner(workspace, config, sandbox=sandbox, project=project,
                        progress=lambda e: job.add_event(
                            {"kind": "gate_progress", **e}))
    report = runner.run()
    report.write_json(workspace / WORK_DIR_NAME / "reports" / f"{report.run_id}.json")
    badges = compute_badges(report, workspace)
    (workspace / WORK_DIR_NAME / "badges.json").write_text(
        json.dumps([asdict(b) for b in badges], indent=2), encoding="utf-8")
    return describe_workspace(workspace, report=report, badges=badges)


# -- serialisation ----------------------------------------------------------------------

def serialize_run(result) -> dict:
    """A pipeline :class:`RunResult` as plain JSON for the UI."""
    workspace = Path(result.workspace)
    report = _report_dict(result.report) if result.report is not None else None
    if result.submission is not None:
        files = [_file_entry(f.path, f.content, f.is_test)
                 for f in result.submission.files]
    else:
        files = []
    return {
        "verdict": result.verdict,
        "verdict_note": result.verdict_note,
        "initial_verdict": result.initial_verdict,
        "passed": result.passed,
        "failure": result.failure,
        "mode": result.mode,
        "provider": result.provider,
        "duration_s": round(result.duration_s, 1),
        "cost": result.cost,
        "workspace": str(workspace),
        "report": report,
        "badges": [asdict(b) for b in result.badges],
        "files": files,
        "spec": result.spec.to_dict() if result.spec else None,
        "specs": {k: v.to_dict() for k, v in result.specs.items()},
        "plan": result.plan.to_dict() if result.plan else None,
        "repairs": [asdict(step) for step in result.repairs],
        "agent_attempts": result.agent_attempts,
        "contract_rejections": list(result.contract_rejections),
        "unverified_claim": result.unverified_claim(),
        "usage": _read(workspace / USAGE_FILE),
    }


def describe_workspace(path: Path, *, report=None, badges=None) -> dict:
    """The same shape as :func:`serialize_run`, rebuilt from files on disk."""
    report_dict = _report_dict(report) if report is not None else _latest_report(path)
    if badges is None:
        raw = _read(path / WORK_DIR_NAME / "badges.json")
        badge_list = json.loads(raw) if raw else []
    else:
        badge_list = [asdict(b) for b in badges]

    from ..gates.base import _is_test, is_excluded_part

    files = []
    for file in sorted(path.rglob("*.py")):
        rel = file.relative_to(path)
        if any(is_excluded_part(part) for part in rel.parts) or rel.name == "conftest.py":
            continue
        files.append(_file_entry(rel.as_posix(), _read(file) or "", _is_test(rel)))

    spec = _read(path / WORK_DIR_NAME / "spec.json")
    specs = {}
    specs_dir = path / WORK_DIR_NAME / "specs"
    if specs_dir.is_dir():
        for spec_file in sorted(specs_dir.glob("*.json")):
            specs[spec_file.stem] = json.loads(spec_file.read_text(encoding="utf-8"))
    plan = _read(path / WORK_DIR_NAME / "plan.json")
    review = Review.load(path)
    return {
        "verdict": report_dict.get("verdict") if report_dict else "NOT VERIFIED",
        "verdict_note": review.caveat() if review and report_dict else None,
        "initial_verdict": None,
        "passed": bool(report_dict and report_dict.get("passed")),
        "failure": None,
        "mode": "multi" if spec else ("project" if specs else "unknown"),
        "provider": "",
        "duration_s": (report_dict or {}).get("duration_s", 0),
        "cost": {},
        "workspace": str(path),
        "report": report_dict,
        "badges": badge_list,
        "files": files,
        "spec": json.loads(spec) if spec else None,
        "specs": specs,
        "plan": json.loads(plan) if plan else None,
        "repairs": [],
        "agent_attempts": 0,
        "contract_rejections": [],
        "unverified_claim": None,
        "usage": _read(path / USAGE_FILE),
        "sources": source_files(path),
    }


def _report_dict(report) -> dict:
    payload = report.to_dict()
    payload.update(passed=report.passed, verdict=report.verdict(),
                   counts=report.counts())
    return payload


def _latest_report(path: Path) -> dict | None:
    reports = path / WORK_DIR_NAME / "reports"
    if not reports.is_dir():
        return None
    candidates = sorted(reports.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for candidate in reversed(candidates):
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def _file_entry(path: str, content: str, is_test: bool) -> dict:
    truncated = len(content) > MAX_FILE_CHARS
    return {"path": path, "role": "test" if is_test else "source",
            "lines": len(content.splitlines()),
            "content": content[:MAX_FILE_CHARS], "truncated": truncated}


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _title(job: Job) -> str:
    if job.kind == "build":
        text = " ".join(job.params.get("requirement", "").split())
        return text[:80] + ("..." if len(text) > 80 else "")
    return f"{job.kind} {Path(job.workspace).name}"


def _cap(value: Any, depth: int = 0) -> Any:
    """Bound an event's size: traces can carry whole prompts."""
    if isinstance(value, str):
        return value if len(value) <= _STRING_CAP else value[:_STRING_CAP] + "â€¦"
    if depth > 4:
        return str(value)[:200]
    if isinstance(value, dict):
        return {str(k): _cap(v, depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, (list, tuple)):
        return [_cap(v, depth + 1) for v in list(value)[:60]]
    return value
