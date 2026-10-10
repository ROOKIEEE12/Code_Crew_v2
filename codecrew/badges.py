"""Per-file trust badges, derived only from a gate report.

The workspace verdict answers "can I trust this project?". A badge answers
the question a reviewer actually has when opening one file: "was *this*
file checked, and what did the checks say about it?".

Badges never add information the gates did not produce, and a file's badge
is never better than the workspace verdict:

``UNVERIFIED``
    A blocking gate failed and its findings point at this file (a failing
    test is attributed to the module it tests), or a blocking gate failed
    without pointing anywhere, which leaves no file trustworthy.
``PARTIALLY VERIFIED``
    Nothing blocking points here, but the run as a whole did not pass, a
    gate was skipped, an advisory gate failed on this file, or a gate that
    passed still reported a high-severity finding in it.
``VERIFIED``
    Every gate ran and passed, and none reported anything serious here.

Lower-severity findings from gates that passed (a surviving mutant, a
partly covered branch) are attached as notes. They are worth reading but
the thresholds that define a pass have already weighed them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from .config import WORK_DIR_NAME
from .results import GateReport, Severity, Status

VERIFIED = "VERIFIED"
PARTIAL = "PARTIALLY VERIFIED"
UNVERIFIED = "UNVERIFIED"

_RANK = {VERIFIED: 0, PARTIAL: 1, UNVERIFIED: 2}

REVIEW_FILE = "review.json"


@dataclass
class FileBadge:
    path: str
    badge: str
    #: Why the badge is not better, one line per cause. Empty when VERIFIED.
    reasons: list[str] = field(default_factory=list)
    #: Findings that did not change the badge but are worth a look.
    notes: list[str] = field(default_factory=list)


@dataclass
class Review:
    """What a person checked before the code was written.

    The gates verify the code *against the spec*. If nobody read the spec,
    a VERIFIED result means "matches the model's reading of the
    requirement", which is less than it sounds like.
    """

    reviewed: bool
    assumptions: int = 0

    def caveat(self) -> str | None:
        if self.reviewed or self.assumptions == 0:
            return None
        noun = "assumption" if self.assumptions == 1 else "assumptions"
        return (f"against an unreviewed spec with {self.assumptions} "
                f"open {noun}")

    def write(self, workspace: Path) -> None:
        path = workspace / WORK_DIR_NAME / REVIEW_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, workspace: Path) -> "Review | None":
        path = workspace / WORK_DIR_NAME / REVIEW_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(reviewed=bool(data["reviewed"]),
                       assumptions=int(data.get("assumptions", 0)))
        except (OSError, ValueError, KeyError, TypeError):
            return None


def source_files(workspace: Path) -> list[str]:
    """Workspace-relative POSIX paths of the code under test (not tests)."""
    from .gates.base import EXCLUDED_DIRS, _is_test

    out = []
    for path in sorted(workspace.rglob("*.py")):
        rel = path.relative_to(workspace)
        if any(part in EXCLUDED_DIRS for part in rel.parts) or _is_test(rel):
            continue
        out.append(rel.as_posix())
    return out


def compute_badges(report: GateReport, workspace: Path,
                   files: list[str] | None = None) -> list[FileBadge]:
    workspace = workspace.resolve()
    files = source_files(workspace) if files is None else files
    badges = {path: FileBadge(path, VERIFIED) for path in files}

    def downgrade(path: str, badge: str, reason: str) -> None:
        entry = badges[path]
        if _RANK[badge] > _RANK[entry.badge]:
            entry.badge = badge
        if reason not in entry.reasons:
            entry.reasons.append(reason)

    def everywhere(badge: str, reason: str) -> None:
        for path in badges:
            downgrade(path, badge, reason)

    for result in report.results:
        label = f"{result.gate_id} {result.name}"
        if result.status is Status.SKIP:
            everywhere(PARTIAL, f"{label} did not run ({result.note or 'skipped'})")
            continue

        located: dict[str, list[str]] = {}
        unlocated = 0
        for finding in result.findings:
            target = _attribute(finding.file, workspace, files)
            text = f"{label}: {finding.message}"
            if finding.line:
                text += f" (line {finding.line})"
            if target is None:
                unlocated += 1
                continue
            if result.blocked:
                downgrade(target, UNVERIFIED, text)
            elif result.status is not Status.PASS:
                downgrade(target, PARTIAL, text)
            elif finding.severity.rank >= Severity.HIGH.rank:
                downgrade(target, PARTIAL, text)
            elif finding.severity.rank >= Severity.MEDIUM.rank:
                located.setdefault(target, []).append(text)

        if result.blocked and (unlocated or not result.findings):
            # A failure nobody can place (a crashed gate, an unattributable
            # test) leaves every file in doubt.
            everywhere(UNVERIFIED, f"{label} failed: {result.summary}")
        for target, notes in located.items():
            badges[target].notes.extend(notes)

    if not report.passed:
        for entry in badges.values():
            if entry.badge == VERIFIED:
                downgrade(entry.path, PARTIAL,
                          "the workspace as a whole did not pass")
    return list(badges.values())


def summarize(badges: list[FileBadge]) -> dict[str, int]:
    counts = {VERIFIED: 0, PARTIAL: 0, UNVERIFIED: 0}
    for badge in badges:
        counts[badge.badge] += 1
    return counts


def _attribute(raw: str | None, workspace: Path, files: list[str]) -> str | None:
    """Map a finding's file (absolute, relative, or a test) to a source file."""
    if not raw:
        return None
    text = raw.replace("\\", "/")
    path = Path(text)
    if path.is_absolute():
        try:
            text = path.resolve().relative_to(workspace).as_posix()
        except ValueError:
            return None
    text = text.removeprefix("./")
    if text in files:
        return text

    # A test file is attributed to the module it is named after:
    # tests/test_stats.py -> stats.py (or pkg/stats.py).
    stem = PurePosixPath(text).name.removesuffix(".py")
    stem = stem.removeprefix("test_").removesuffix("_test")
    matches = [f for f in files if PurePosixPath(f).name == f"{stem}.py"]
    if len(matches) == 1:
        return matches[0]
    if len(files) == 1:
        # One module under test: every finding is about it.
        return files[0]
    return None
