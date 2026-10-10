"""CLI for the gate layer: ``python -m codecrew.gates run <workspace>``.

The gate layer is usable standalone, with no LLM involved. That is
deliberate: it can score a hand-written project, which is how we validate
the judge before trusting it to judge generated code.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..badges import Review, compute_badges
from ..config import WORK_DIR_NAME, GateConfig
from ..report import render, render_markdown
from ..sandbox import get_sandbox
from .runner import GateRunner, all_gate_ids


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m codecrew.gates",
        description="Run CodeCrew's deterministic verification gates.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="verify a workspace")
    run.add_argument("workspace", type=Path, help="directory to verify")
    run.add_argument("--config", type=Path, default=None,
                     help="JSON config file with thresholds")
    run.add_argument("--only", default=None,
                     help="comma-separated gate ids to run, e.g. G1,G2")
    run.add_argument("--disable", default=None,
                     help="comma-separated gate ids to skip")
    run.add_argument("--sandbox", choices=("auto", "docker", "local"), default=None,
                     help="execution backend (default: auto)")
    run.add_argument("--min-coverage", type=float, default=None,
                     help="override the branch coverage threshold")
    run.add_argument("--min-mutation", type=float, default=None,
                     help="override the mutation score threshold")
    run.add_argument("--max-mutants", type=int, default=None,
                     help="cap the number of mutants evaluated")
    run.add_argument("--json", type=Path, default=None,
                     help="also write the machine-readable report here")
    run.add_argument("--markdown", type=Path, default=None,
                     help="also write a Markdown summary here (for a CI job "
                          "summary, e.g. $GITHUB_STEP_SUMMARY)")
    run.add_argument("--quiet", action="store_true",
                     help="print only the verdict line")

    sub.add_parser("gates", help="list the gates and exit")
    return parser


def _resolve_config(args: argparse.Namespace) -> GateConfig:
    config = GateConfig.load(args.config)
    if args.sandbox:
        config.sandbox = args.sandbox
    if args.disable:
        config.disabled = tuple(_ids(args.disable))
    if args.min_coverage is not None:
        config.thresholds.min_branch_coverage = args.min_coverage
    if args.min_mutation is not None:
        config.thresholds.min_mutation_score = args.min_mutation
    if args.max_mutants is not None:
        config.thresholds.max_mutants = args.max_mutants
    return config


def _ids(raw: str) -> list[str]:
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "gates":
        from .runner import GATE_CLASSES
        for cls in GATE_CLASSES:
            blocking = "blocking" if cls.blocking else "advisory"
            deps = ",".join(cls.depends_on) or "-"
            print(f"{cls.gate_id}  {cls.name:28} {blocking:9} needs:{deps}")
        return 0

    workspace: Path = args.workspace
    if not workspace.is_dir():
        print(f"error: not a directory: {workspace}", file=sys.stderr)
        return 2

    config = _resolve_config(args)
    only = _ids(args.only) if args.only else None
    if only:
        unknown = set(only) - set(all_gate_ids())
        if unknown:
            print(f"error: unknown gate id(s): {', '.join(sorted(unknown))}",
                  file=sys.stderr)
            return 2

    sandbox = get_sandbox(workspace.resolve(), config.sandbox)
    runner = GateRunner(workspace, config, sandbox=sandbox)
    report = runner.run(only=only)

    report_path = args.json or (
        workspace / WORK_DIR_NAME / "reports" / f"{report.run_id}.json"
    )
    report.write_json(report_path)

    badges = compute_badges(report, workspace) if not only else []
    review = Review.load(workspace.resolve())
    note = review.caveat() if review else None
    if args.markdown:
        # Appends, because $GITHUB_STEP_SUMMARY is shared by every step.
        with args.markdown.open("a", encoding="utf-8") as handle:
            handle.write(render_markdown(report, badges, note=note))

    if args.quiet:
        print(f"{report.verdict()}  ({report.run_id})")
    else:
        print(render(report, isolation=sandbox.isolation_notes()))
        if note:
            print(f"\nNOTE: {report.verdict()} {note}.")
        if badges:
            print("\nTRUST PER FILE")
            for badge in badges:
                print(f"  {badge.badge:20} {badge.path}")
        print(f"\nmachine-readable report: {report_path}")

    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
