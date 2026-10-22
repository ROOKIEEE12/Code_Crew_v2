"""G8 - dependency sanity: are the imports real, declared, and safe?

AI models hallucinate package names, and an attacker can register the name
a model tends to hallucinate ("slopsquatting"), so an import of a package
nobody vetted is a supply-chain risk, not a style issue.

Two tiers, kept apart so that one never pretends to be the other:

* **Offline, always run.** Every third-party import in the code must have
  been declared in the approved plan. This alone catches a hallucinated
  dependency, needs no network, and also verifies the common good case:
  "standard library only" is checked, not assumed.
* **Online, when PyPI is reachable.** A declared package must exist, must
  not carry known vulnerabilities, and is flagged when brand new (the
  slopsquatting signature) or abandoned. If PyPI cannot be reached the
  gate says so and reports SKIP rather than a clean pass.

Only implementation files are scanned. A test file's imports (pytest,
hypothesis) are the toolchain's, not the project's.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from ..config import WORK_DIR_NAME
from ..results import Finding, GateResult, Severity, Status
from .base import Gate, GateContext

_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ())) | {"__future__"}

#: Import name -> PyPI distribution name, where they differ. Anything not
#: listed is assumed to be the same, which is right for most packages.
IMPORT_TO_DIST = {
    "yaml": "PyYAML", "PIL": "Pillow", "bs4": "beautifulsoup4",
    "cv2": "opencv-python", "sklearn": "scikit-learn", "dateutil": "python-dateutil",
    "jwt": "PyJWT", "dotenv": "python-dotenv", "serial": "pyserial",
    "OpenSSL": "pyOpenSSL", "Crypto": "pycryptodome", "attr": "attrs",
}

NEW_PACKAGE_DAYS = 30
ABANDONED_DAYS = 3 * 365
_PYPI = "https://pypi.org/pypi/{name}/json"


class NetworkUnavailable(RuntimeError):
    """PyPI could not be reached. Distinct from the package not existing."""


class PackageMissing(LookupError):
    """PyPI answered 404: no such package."""


def _normalise(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def fetch_pypi(name: str, timeout: int = 10) -> dict:
    """Return PyPI's JSON for ``name``.

    Module-level so tests (and an air-gapped run) can substitute it.
    """
    request = urllib.request.Request(
        _PYPI.format(name=name),
        headers={"User-Agent": "codecrew/0.1", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise PackageMissing(name) from exc
        raise NetworkUnavailable(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise NetworkUnavailable(str(exc)) from exc


def third_party_imports(ctx: GateContext) -> dict[str, list[str]]:
    """Top-level third-party import names -> where they are imported."""
    local = set(ctx.package_roots())
    found: dict[str, list[str]] = {}
    for rel in ctx.source_files:
        try:
            tree = ast.parse((ctx.workspace / rel).read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for full in names:
                top = full.split(".")[0]
                if top in _STDLIB or top in local:
                    continue
                found.setdefault(top, []).append(f"{rel.as_posix()}:{node.lineno}")
    return found


def declared_packages(ctx: GateContext) -> set[str]:
    """Normalised distribution names the project declares.

    For a generated project that is the approved plan. For a developer's
    project it is what they declared themselves: pyproject dependencies
    (and test/dev extras) and requirements files.
    """
    declared: set[str] = set()
    plan = ctx.workspace / WORK_DIR_NAME / "plan.json"
    if plan.exists():
        try:
            data = json.loads(plan.read_text(encoding="utf-8"))
            declared |= {_normalise(d["name"]) for d in data.get("dependencies", [])
                         if isinstance(d, dict) and "name" in d}
        except (OSError, ValueError):
            pass
    project = ctx.project
    if project is not None:
        declared |= {_normalise(n) for n in project.declared_names()}
        for name in project.requirement_files:
            declared |= _requirement_names(ctx.workspace / name)
    return declared


_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _requirement_names(path, depth: int = 0) -> set[str]:
    names: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return names
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if line.startswith(("-r ", "--requirement ")) and depth < 3:
            names |= _requirement_names(path.parent / line.split(None, 1)[1], depth + 1)
            continue
        if not line or line.startswith("-"):
            continue
        match = _REQ_LINE.match(line)
        if match:
            names.add(_normalise(match.group(1)))
    return names


def installed_distributions(ctx: GateContext) -> dict[str, list[str]]:
    """Import name -> distributions providing it, from the run's environment.

    Exact where the hand-written table is a guess ("dateutil" comes from
    "python-dateutil"). Empty when it cannot be asked, in which case the
    table and the import name itself are used.
    """
    try:
        run = ctx.sandbox.run(
            ["python", "-c",
             "import importlib.metadata as m, json; "
             "print(json.dumps(m.packages_distributions()))"],
            timeout=60)
    except Exception:  # noqa: BLE001 - a lookup aid, never a verdict
        return {}
    if not run.ok:
        return {}
    try:
        data = json.loads(run.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}
    return {k: list(v) for k, v in data.items() if isinstance(v, list)}


class DependencyGate(Gate):
    gate_id = "G8"
    name = "Dependency check"
    blocking = True
    depends_on = ("G1",)

    def check(self, ctx: GateContext) -> GateResult:
        imports = third_party_imports(ctx)
        if not imports:
            return self._result(ctx, Status.PASS,
                                "standard library only; no third-party imports")

        declared = declared_packages(ctx)
        installed = installed_distributions(ctx)
        generated = bool(getattr(ctx.project, "generated", True))
        where = ("the approved plan" if generated
                 else "the project (pyproject.toml or requirements files)")

        def candidates(top: str) -> list[str]:
            return [*installed.get(top, []), IMPORT_TO_DIST.get(top, top), top]

        findings: list[Finding] = []
        undeclared: list[str] = []
        for top, places in sorted(imports.items()):
            if not any(_normalise(c) in declared for c in candidates(top)):
                undeclared.append(top)
                file, _, line = places[0].partition(":")
                findings.append(Finding(
                    severity=Severity.HIGH, code="undeclared-dependency",
                    message=(f"imports '{top}', which {where} never declared; "
                             f"it works here only by accident, or it is a "
                             f"hallucinated or unvetted package"),
                    file=file, line=int(line) if line.isdigit() else None,
                ))

        # The online tier, over every package that is imported, declared or
        # not: an undeclared name that also does not exist is the sharper
        # finding.
        offline_only = False
        for top in sorted(imports):
            dist = candidates(top)[0]
            try:
                findings.extend(_inspect_package(dist))
            except NetworkUnavailable:
                offline_only = True
                break

        blocking = [f for f in findings
                    if f.severity.rank >= Severity.MEDIUM.rank]
        names = ", ".join(sorted(imports))
        if blocking:
            return self._result(
                ctx, Status.FAIL,
                f"{len(blocking)} dependency problem(s) in: {names}",
                findings=findings,
                note="PyPI checks did not run (unreachable)" if offline_only else None,
            )
        if offline_only:
            return self._result(
                ctx, Status.SKIP, "not fully run", findings=findings,
                note=f"{names} are declared, but PyPI was unreachable so "
                     f"existence, vulnerability and age were not checked",
            )
        return self._result(
            ctx, Status.PASS, f"{len(imports)} declared package(s) verified: {names}",
            findings=findings,
        )


def _inspect_package(dist: str) -> list[Finding]:
    """Findings for one distribution, from PyPI. Raises NetworkUnavailable."""
    try:
        data = fetch_pypi(dist)
    except PackageMissing:
        return [Finding(
            severity=Severity.CRITICAL, code="package-does-not-exist",
            message=f"'{dist}' does not exist on PyPI. Models hallucinate "
                    f"package names and attackers register them.",
        )]

    findings: list[Finding] = []
    vulns = [v for v in data.get("vulnerabilities") or []
             if isinstance(v, dict) and not v.get("withdrawn")]
    if vulns:
        ids = ", ".join(str(v.get("id")) for v in vulns[:3])
        findings.append(Finding(
            severity=Severity.HIGH, code="known-vulnerability",
            message=f"'{dist}' has {len(vulns)} known vulnerabilit"
                    f"{'y' if len(vulns) == 1 else 'ies'} ({ids})",
        ))

    uploads = _upload_times(data)
    if uploads:
        now = datetime.now(timezone.utc)
        newest_age = (now - max(uploads)).days
        first_age = (now - min(uploads)).days
        if first_age < NEW_PACKAGE_DAYS:
            findings.append(Finding(
                severity=Severity.LOW, code="very-new-package",
                message=f"'{dist}' first appeared {first_age} day(s) ago; "
                        f"new names are the slopsquatting signature",
            ))
        if newest_age > ABANDONED_DAYS:
            findings.append(Finding(
                severity=Severity.LOW, code="possibly-abandoned",
                message=f"'{dist}' has had no release for {newest_age // 365} "
                        f"year(s)",
            ))
    return findings


def _upload_times(data: dict) -> list[datetime]:
    times = []
    for files in (data.get("releases") or {}).values():
        for entry in files or []:
            stamp = entry.get("upload_time_iso_8601") or entry.get("upload_time")
            if not stamp:
                continue
            try:
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            times.append(parsed if parsed.tzinfo
                         else parsed.replace(tzinfo=timezone.utc))
    return times
