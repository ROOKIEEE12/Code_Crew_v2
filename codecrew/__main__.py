"""CodeCrew CLI: ``python -m codecrew <command>``.

    doctor                      which LLM providers are usable right now
    build "<requirement>"       generate, then verify, into a workspace
    verify <workspace>          run the gates only (same as codecrew.gates)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import GateConfig
from .llm import LLMConfig, ModelTiers, ProviderUnavailable, provider_status
from .report import render_run
from .sandbox import docker_available, get_sandbox


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m codecrew",
        description="CodeCrew: generate code and verify it before you trust it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="show provider and sandbox availability")

    build = sub.add_parser("build", help="generate and verify a requirement")
    build.add_argument("requirement", help="what to build, in plain language")
    build.add_argument("--out", type=Path, required=True,
                       help="workspace directory to generate into")
    build.add_argument("--provider", default="auto",
                       help="auto|groq|gemini|ollama|anthropic|scripted")
    build.add_argument("--script", default=None,
                       help="with --provider scripted: JSON array of canned "
                            "responses to replay (offline, reproducible)")
    build.add_argument("--fast-model", default=None,
                       help="override the model used for mechanical tasks")
    build.add_argument("--strong-model", default=None,
                       help="override the model used for reasoning tasks")
    build.add_argument("--temperature", type=float, default=0.0)
    build.add_argument("--max-tokens", type=int, default=4096)
    build.add_argument("--mode", choices=("single", "multi"), default="multi",
                       help="single: one agent writes code and tests (the "
                            "baseline). multi: spec -> approval -> independent "
                            "implementer and test author")
    build.add_argument("--approve", choices=("ask", "auto"), default="ask",
                       help="multi mode: pause for spec approval (ask) or "
                            "proceed without review (auto)")
    build.add_argument("--repair", type=int, default=2, metavar="N",
                       help="multi mode: up to N reconciliation rounds when "
                            "the gates fail (0 disables). Each round "
                            "diagnoses code/test/spec defect, patches that "
                            "artefact, and re-runs every gate")
    build.add_argument("--spec", type=Path, default=None,
                       help="multi mode: use this spec.json instead of "
                            "generating one (skips the Spec agent)")
    build.add_argument("--clean", action="store_true",
                       help="delete the workspace first")
    build.add_argument("--no-verify", action="store_true",
                       help="generate without running the gates")
    build.add_argument("--sandbox", choices=("auto", "docker", "local"),
                       default="auto")
    build.add_argument("--min-coverage", type=float, default=None)
    build.add_argument("--min-mutation", type=float, default=None)
    build.add_argument("--disable", default=None,
                       help="comma-separated gate ids to skip, e.g. G4")

    verify = sub.add_parser("verify", help="run the gates over an existing workspace")
    verify.add_argument("workspace", type=Path)

    bench = sub.add_parser(
        "bench",
        help="run the benchmark and report the false-confidence rate",
    )
    bench.add_argument("--out", type=Path, default=Path("bench_runs"),
                       help="directory for per-task workspaces and results.json")
    bench.add_argument("--tasks", default=None,
                       help="comma-separated task ids (default: all)")
    bench.add_argument("--list", action="store_true", help="list tasks and exit")
    bench.add_argument("--provider", default="auto",
                       help="auto|groq|gemini|ollama|anthropic")
    bench.add_argument("--fast-model", default=None)
    bench.add_argument("--strong-model", default=None)
    bench.add_argument("--sandbox", choices=("auto", "docker", "local"),
                       default="auto")
    bench.add_argument("--disable", default=None,
                       help="comma-separated gate ids to skip, e.g. G4 "
                            "(much faster, weaker verdict)")
    bench.add_argument("--max-mutants", type=int, default=None)
    bench.add_argument("--repair", type=int, default=0, metavar="N",
                       help="multi mode: reconciliation rounds per task "
                            "(default 0, so the loop's effect is an explicit "
                            "ablation and not mixed into the base result)")
    bench.add_argument("--mode", choices=("single", "multi"), default="single",
                       help="single: one agent writes code and tests. multi: "
                            "independent implementer and test author, spec "
                            "auto-approved")

    compare = sub.add_parser(
        "compare", help="compare two benchmark results.json files side by side")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)

    return parser


def _doctor() -> int:
    print("LLM providers")
    print("-" * 60)
    usable = 0
    for name, ok, reason in provider_status():
        if ok:
            usable += 1
            print(f"  [ready]   {name}")
        else:
            print(f"  [missing] {name:10} {reason}")
    print(f"  [ready]   scripted   (offline, no model required)")

    print("\nSandbox")
    print("-" * 60)
    if docker_available():
        print("  [ready]   docker     enforced isolation: no network, read-only FS")
    else:
        print("  [missing] docker     not available")
        print("  [ready]   local-venv dependency isolation only; generated code")
        print("                       can reach the network and host filesystem")

    if usable == 0:
        print("\nNo live provider configured. Set GROQ_API_KEY or GEMINI_API_KEY")
        print("(both have free tiers), or run Ollama locally. You can still use")
        print("--provider scripted and `verify` with no model at all.")
    return 0


def _interactive_approver(out_dir: Path):
    """The human checkpoint: review the spec before any code exists."""

    def approve(spec):
        print("\n" + "=" * 74)
        print("SPECIFICATION - review before any code is written")
        print("=" * 74)
        print(spec.render())
        print("=" * 74)
        print("Pay most attention to the ASSUMPTIONS: each is a place the "
              "requirement\nwas silent and the model had to guess.\n")
        while True:
            answer = input("Approve? [y]es / [n]o / [e]dit as file: ").strip().lower()
            if answer in ("y", "yes"):
                return spec
            if answer in ("n", "no"):
                return None
            if answer in ("e", "edit"):
                draft = out_dir / ".codecrew" / "spec.draft.json"
                draft.parent.mkdir(parents=True, exist_ok=True)
                draft.write_text(spec.to_json(), encoding="utf-8")
                print(f"\nWrote {draft}\nEdit it, then re-run with "
                      f"--spec {draft}")
                return None

    return approve


def _build(args: argparse.Namespace) -> int:
    tiers = None
    if args.fast_model or args.strong_model:
        tiers = ModelTiers(fast=args.fast_model, strong=args.strong_model)

    llm_config = LLMConfig(
        provider=args.provider,
        script=args.script,
        tiers=tiers,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )
    gate_config = GateConfig(sandbox=args.sandbox)
    if args.disable:
        gate_config.disabled = tuple(
            part.strip().upper() for part in args.disable.split(",") if part.strip()
        )
    if args.min_coverage is not None:
        gate_config.thresholds.min_branch_coverage = args.min_coverage
    if args.min_mutation is not None:
        gate_config.thresholds.min_mutation_score = args.min_mutation

    from .pipeline import Pipeline

    supplied_spec = None
    if args.spec is not None:
        from .agents import ContractError, Spec

        try:
            supplied_spec = Spec.parse(args.spec.read_text(encoding="utf-8"))
        except (OSError, ContractError) as exc:
            print(f"error: cannot use spec {args.spec}: {exc}", file=sys.stderr)
            return 2

    approver = None
    if args.mode == "multi" and args.approve == "ask":
        if not sys.stdin.isatty():
            print("error: spec approval needs a terminal. Re-run with "
                  "--approve auto, or supply a reviewed --spec.",
                  file=sys.stderr)
            return 2
        approver = _interactive_approver(args.out)

    try:
        pipeline = Pipeline(llm_config=llm_config, gate_config=gate_config)
        result = pipeline.build(
            args.requirement, args.out,
            mode=args.mode, spec=supplied_spec, approve=approver,
            repair_rounds=args.repair if args.mode == "multi" else 0,
            clean=args.clean, verify=not args.no_verify,
        )
    except ProviderUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    isolation = None
    if result.report is not None:
        isolation = get_sandbox(result.workspace, gate_config.sandbox).isolation_notes()
    print(render_run(result, isolation=isolation))
    return 0 if result.passed else 1


def _bench(args: argparse.Namespace) -> int:
    from .bench import (
        TASKS,
        BenchRunner,
        get_tasks,
        render_summary,
        summarize,
        write_results,
    )

    if args.list:
        for task in TASKS:
            print(f"{task.id:16} {task.tier:9} {task.module}")
        return 0

    try:
        tasks = get_tasks(
            [t.strip() for t in args.tasks.split(",") if t.strip()]
            if args.tasks else None
        )
    except KeyError as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 2

    tiers = None
    if args.fast_model or args.strong_model:
        tiers = ModelTiers(fast=args.fast_model, strong=args.strong_model)
    llm_config = LLMConfig(provider=args.provider, tiers=tiers)
    gate_config = GateConfig(sandbox=args.sandbox)
    if args.disable:
        gate_config.disabled = tuple(
            p.strip().upper() for p in args.disable.split(",") if p.strip()
        )
    if args.max_mutants is not None:
        gate_config.thresholds.max_mutants = args.max_mutants

    try:
        runner = BenchRunner(llm_config, gate_config, args.out, mode=args.mode,
                             repair_rounds=args.repair)
        outcomes = runner.run(tasks)
    except ProviderUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    summary = summarize(outcomes)
    print()
    print(render_summary(outcomes, summary))
    results = runner.out_dir / "results.json"
    write_results(outcomes, summary, results, meta={
        "mode": args.mode,
        "repair_rounds": args.repair,
        "provider": args.provider,
        "disabled_gates": list(gate_config.disabled),
        "max_mutants": gate_config.thresholds.max_mutants,
    })
    print(f"\nresults: {results}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    from .gates.__main__ import main as gates_main

    return gates_main(["run", str(args.workspace)])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        return _doctor()
    if args.command == "build":
        return _build(args)
    if args.command == "verify":
        return _verify(args)
    if args.command == "bench":
        return _bench(args)
    if args.command == "compare":
        from .bench import compare_results

        try:
            print(compare_results(args.baseline, args.candidate))
        except (OSError, ValueError, KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
