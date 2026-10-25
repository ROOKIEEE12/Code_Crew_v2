"""G4 - mutation testing: do the tests actually detect bugs?

This is the gate that makes the project more than a prompt chain. G2 proves
the tests pass; only G4 proves they would have *failed* had the code been
wrong. A suite with 100% pass rate and a 10% mutation score is decoration.
"""

from __future__ import annotations

import dataclasses
import shutil
import tempfile
import time
from pathlib import Path

from .. import mutation as engine
from ..results import Finding, GateResult, Severity, Status
from .base import Gate, GateContext


class MutationGate(Gate):
    gate_id = "G4"
    name = "Mutation score"
    blocking = True
    requires_module = "pytest"
    depends_on = ("G2",)

    def check(self, ctx: GateContext) -> GateResult:
        if not ctx.test_files:
            return self._result(ctx, Status.SKIP, "not run",
                                note="no tests to evaluate")

        thresholds = ctx.config.thresholds
        all_sites = engine.collect_sites(ctx.workspace, ctx.source_files)
        if not all_sites:
            return self._result(
                ctx, Status.SKIP, "not run",
                note="no mutable logic found (no comparisons, arithmetic, "
                     "literals or returns to mutate)",
            )
        sites = engine.sample_sites(all_sites, thresholds.max_mutants)

        # Mutants are applied to a scratch copy, never to the project: an
        # editor, a file watcher or a crash mid-run must not see (or keep)
        # deliberately broken code in someone's working tree.
        scratch = Path(tempfile.mkdtemp(prefix="codecrew-mutants-"))
        try:
            shutil.copytree(ctx.workspace, scratch / "w", ignore=_ignore_excluded,
                            dirs_exist_ok=True)
            mctx = dataclasses.replace(ctx, workspace=scratch / "w",
                                       sandbox=ctx.sandbox.for_workspace(scratch / "w"))
            return self._mutate(mctx, ctx, sites, len(all_sites))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _mutate(self, ctx: GateContext, home: GateContext,
                sites: list[engine.MutationSite], total_sites: int) -> GateResult:
        """Score ``sites`` in the scratch context; evidence goes to ``home``."""
        thresholds = ctx.config.thresholds

        # A green baseline is required: if the suite is already failing, a
        # "killed" mutant proves nothing. G2 normally catches this first.
        baseline = self._run_tests(ctx, thresholds.mutant_timeout_s)
        if not baseline.ok:
            return self._result(
                ctx, Status.SKIP, "not run", evidence=self.save_evidence(
                    home, baseline, suffix="-baseline"),
                note="test suite does not pass, so mutation score is meaningless",
            )

        killed: list[engine.MutationSite] = []
        survived: list[engine.MutationSite] = []
        errored: list[engine.MutationSite] = []
        deadline = time.monotonic() + thresholds.mutation_timeout_s
        truncated = False

        for site in sites:
            if time.monotonic() > deadline:
                truncated = True
                break
            outcome = self._evaluate(ctx, site, thresholds.mutant_timeout_s)
            if outcome == "killed":
                killed.append(site)
            elif outcome == "survived":
                survived.append(site)
            else:
                errored.append(site)

        evaluated = len(killed) + len(survived)
        if evaluated == 0:
            return self._result(
                ctx, Status.ERROR, "no mutant could be evaluated",
                note=f"{len(errored)} mutant(s) failed to build or run",
            )

        score = 100.0 * len(killed) / evaluated
        threshold = thresholds.min_mutation_score
        findings = [
            Finding(
                severity=Severity.MEDIUM,
                code=f"survived-mutant:{site.operator}",
                message=f"Tests still pass with this change applied "
                        f"({site.description}) - behaviour is unconstrained",
                file=site.file,
                line=site.lineno,
            )
            for site in survived
        ]
        evidence = self._write_report(
            home, killed, survived, errored, score, total_sites, truncated
        )
        summary = (
            f"{score:.1f}% mutation score "
            f"({len(killed)}/{evaluated} killed, threshold {threshold:.0f}%)"
        )
        notes = []
        if len(sites) < total_sites:
            notes.append(f"sampled {len(sites)} of {total_sites} sites")
        if truncated:
            notes.append("stopped early on the gate time limit")
        if errored:
            notes.append(f"{len(errored)} mutant(s) could not be evaluated")

        status = Status.FAIL if score + 1e-9 < threshold else Status.PASS
        return self._result(ctx, status, summary, score=score, findings=findings,
                            evidence=evidence,
                            note="; ".join(notes) if notes else None)

    # -- internals -------------------------------------------------------------

    def _run_tests(self, ctx: GateContext, timeout: int):
        """Run the suite as fast as possible: first failure is enough."""
        args = ["python", "-m", "pytest", ctx.test_root, "-x", "-q",
                "-p", "no:cacheprovider", "--no-header"]
        if ctx.sandbox.has_module("pytest_timeout"):
            args += ["--timeout", str(max(5, timeout - 5))]
        return ctx.sandbox.run(args, timeout=timeout)

    def _evaluate(self, ctx: GateContext, site: engine.MutationSite, timeout: int) -> str:
        """Apply one mutant, run the suite, always restore the original."""
        target = ctx.workspace / site.file
        backup = target.with_suffix(target.suffix + ".codecrew-bak")
        try:
            mutated = engine.mutated_source(ctx.workspace, site)
        except (SyntaxError, OSError, ValueError, IndexError):
            return "error"

        shutil.copy2(target, backup)
        try:
            target.write_text(mutated, encoding="utf-8")
            result = self._run_tests(ctx, timeout)
        finally:
            # Restoring is non-negotiable: a crash here would leave the
            # user's workspace holding deliberately broken code.
            shutil.copy2(backup, target)
            _unlink(backup)
            _clear_pycache(ctx.workspace)

        if result.timed_out:
            # A mutant that hangs is detected by the suite's own time limit.
            return "killed"
        return "killed" if result.returncode != 0 else "survived"

    def _write_report(
        self,
        ctx: GateContext,
        killed: list[engine.MutationSite],
        survived: list[engine.MutationSite],
        errored: list[engine.MutationSite],
        score: float,
        total_sites: int,
        truncated: bool,
    ) -> str:
        ctx.evidence_dir.mkdir(parents=True, exist_ok=True)
        path = ctx.evidence_dir / "G4-mutants.log"
        lines = [
            "MUTATION TESTING REPORT",
            f"score            : {score:.1f}%",
            f"mutation sites   : {total_sites} found, "
            f"{len(killed) + len(survived) + len(errored)} evaluated"
            + (" (time limit reached)" if truncated else ""),
            f"killed           : {len(killed)}",
            f"survived         : {len(survived)}",
            f"not evaluated    : {len(errored)}",
            "",
            "SURVIVED (tests did not notice these changes):",
        ]
        lines += [f"  - {s.label}" for s in survived] or ["  (none)"]
        lines += ["", "KILLED (tests correctly failed):"]
        lines += [f"  - {s.label}" for s in killed] or ["  (none)"]
        if errored:
            lines += ["", "NOT EVALUATED:"]
            lines += [f"  - {s.label}" for s in errored]
        path.write_text("\n".join(lines), encoding="utf-8")
        try:
            return path.relative_to(ctx.workspace).as_posix()
        except ValueError:
            return path.as_posix()


def _ignore_excluded(directory: str, names: list[str]) -> set[str]:
    from .base import is_excluded_part
    return {name for name in names if is_excluded_part(name)}


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _clear_pycache(workspace: Path) -> None:
    """Stale bytecode would mask a mutant, inflating the score."""
    for cache in workspace.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
