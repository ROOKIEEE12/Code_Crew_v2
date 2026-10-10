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

    try:
        pipeline = Pipeline(llm_config=llm_config, gate_config=gate_config)
        result = pipeline.build(
            args.requirement, args.out,
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
        runner = BenchRunner(llm_config, gate_config, args.out)
        outcomes = runner.run(tasks)
    except ProviderUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    summary = summarize(outcomes)
    print()
    print(render_summary(outcomes, summary))
    results = runner.out_dir / "results.json"
    write_results(outcomes, summary, results, meta={
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
