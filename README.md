# CodeCrew

A multi-agent AI system for collaborative software development — built as a
**verification pipeline that happens to use AI to write the code**, rather than
a prompt chain that trusts its own output.

## The problem this exists to solve

A single-agent coding assistant generates code and stops. The obvious fix —
add a Testing Agent and a Debugging Agent — does not actually work, because
the agent that writes the tests and the agent that fixes the code are the same
kind of thing as the agent that wrote the bug. An LLM grading its own homework
with a rubric it also wrote will always pass.

The evidence that this matters:

| Finding | Source |
|---|---|
| ~44% of AI code-generation tasks introduce a security vulnerability; pass rate moved 55% → 56% in a year | [Veracode 2026 GenAI Code Security Report](https://www.veracode.com/blog/2026-genai-code-security-report-ai-risk/) |
| 45–70% of AI-generated samples fail security tests; hardcoded credentials and authorization flaws dominate | [Cloud Security Alliance](https://labs.cloudsecurityalliance.org/research/csa-research-note-ai-codegen-vulnerability-debt-20260406-csa/) |
| CVEs traced directly to AI-generated code: 6 (Jan 2026) → 15 (Feb) → 35 (Mar) | [CSA CVE surge note](https://labs.cloudsecurityalliance.org/research/csa-research-note-ai-generated-code-vulnerability-surge-2026/) |
| Security *degrades* across refinement iterations — the debug loop makes it worse | [Security Degradation in Iterative AI Code Generation](https://arxiv.org/pdf/2506.11022) |
| Passing functional tests is not sufficient evidence of a correct change | [SWE-Gate](https://arxiv.org/pdf/2609.04167) |

## The core design rule

> **No agent may declare its own success.** Every "pass" in CodeCrew comes from
> a deterministic tool — a compiler, a linter, a static analyzer, a real test
> run in a sandbox, a coverage number, a mutation score.

Three structural consequences:

1. **The gate layer contains no LLM calls.** Not now, not later.
2. **The Coder and the Test-Author read only the spec**, never each other's
   output, so tests cannot be shaped to fit the code.
3. **Every gate re-runs after every patch**, never just the one that failed —
   directly because of the iterative-security-degradation finding above.

## Status: Phase 0 complete

The gate layer is finished and runs standalone with **no LLM and no API key**.
This ordering is deliberate: the judge is built and validated before anything
is judged.

## Quick start

```bash
# One-time: build the shared tool environment (~1-2 min)
python -c "from pathlib import Path; from codecrew.sandbox.local import LocalSandbox; LocalSandbox(Path('.')).ensure()"

# Verify a workspace
python -m codecrew.gates run samples/strength_good    # -> VERIFIED,   exit 0
python -m codecrew.gates run samples/strength_vibe    # -> UNVERIFIED, exit 1

# List the gates
python -m codecrew.gates gates
```

Useful flags: `--only G1,G2`, `--disable G4`, `--sandbox docker`,
`--min-coverage 85`, `--min-mutation 70`, `--max-mutants 100`, `--json out.json`,
`--quiet`.

## The gates

| ID | Gate | Blocking | Checks |
|---|---|---|---|
| G1 | Syntax, lint and types | yes | `compileall`, `ruff`, `mypy` |
| G2 | Test execution | yes | `pytest` in a sandbox; **no tests = FAIL, not SKIP** |
| G3 | Branch coverage | yes | branch (not line) coverage vs threshold |
| G5 | Security scan | yes | `bandit` + `detect-secrets`; secrets are always HIGH |
| G4 | Mutation score | yes | do the tests actually detect injected bugs? |
| G6 | Property-based coverage | no | share of public functions with a Hypothesis test |

Gates run cheapest-first so a failing patch gets feedback in seconds. A gate
whose dependency failed reports `SKIP — blocked by G1` instead of running
against code that cannot compile, but independent gates still run, so one
failure never hides every other problem.

### Three policy decisions worth defending in a viva

- **`SKIP` is never a pass.** A missing tool downgrades the verdict to
  `PARTIALLY VERIFIED` and is listed under "what was not verified". Silent
  green on an unrun check is the exact failure mode this project targets.
- **Code with no tests FAILs.** It is not "nothing to check" — it is the output
  the project exists to prevent.
- **A crashed gate is `ERROR`, not `FAIL`.** A bug in CodeCrew must never be
  reported as a defect in the code under test, and must never read as clean.

## Mutation testing: the headline metric

G4 is what separates this from a prompt chain. G2 proves the tests pass; only
G4 proves they would have **failed** had the code been wrong.

A mutant is one small change to the code under test — a flipped comparison, a
swapped operator, a shifted constant, a discarded return value. If the suite
still passes with the change applied, the mutant *survived*: that behaviour is
unconstrained by the tests.

The engine ([`codecrew/mutation.py`](codecrew/mutation.py)) is written from
scratch rather than delegated to an off-the-shelf runner, because the score is
the project's central claim and needs to be inspectable; because existing
runners are fragile on Windows and hard to time-bound; and because Phase 2
needs to hand the Test-Author agent the *specific* surviving mutant to fix.
Sampling uses a fixed seed so scores stay comparable across runs — which the
ablation study depends on.

### What it found, unprompted

The two sample workspaces expose the same API. One is honest; one is
"vibe-coded" — it runs, and it passes its own test suite.

```
samples/strength_good   VERIFIED      G2: 16 passed   G3: 100.0%   G4: 95.7%
samples/strength_vibe   UNVERIFIED    G2:  3 passed   G3:  71.8%   G4: 30.8%
```

The vibe-coded workspace **passed G2 with a green suite while being broken.**
The gates reported:

- `strength.py:26 survived-mutant: > -> >=` — this *is* the off-by-one bug,
  located with no prior knowledge of it
- 4 security findings at or above medium: two hardcoded credentials and an
  `eval` on caller input
- 0 of 4 public functions covered by a property test

That is the whole thesis, reproducible with one command.

## Sandboxing

All generated code executes through a `Sandbox`. Two backends with honestly
different guarantees, and the report always states which one ran:

- **`docker`** (preferred, auto-detected) — `--network none`, read-only
  filesystem, capped CPU/memory/PIDs, non-root user, per-command timeout.
- **`local-venv`** (fallback) — isolates *dependencies* only. Generated code
  can still reach the network and the host filesystem. The report says so in
  plain language rather than implying safety it does not provide.

## Layout

```
codecrew/
  config.py          thresholds and gate toggles (all tunable for ablations)
  results.py         Status/Severity/Finding/GateResult/GateReport + verdict policy
  mutation.py        the AST mutation engine
  report.py          human-readable verification report
  sandbox/           base contract, docker backend, local-venv backend
  gates/
    base.py          Gate contract, workspace discovery, evidence capture
    syntax.py        G1      tests.py     G2      coverage.py  G3
    security.py      G5      mutation.py  G4      properties.py G6
    runner.py        sequencing, dependency skips, aggregation
    __main__.py      CLI
samples/
  strength_good/     reference workspace: should pass
  strength_vibe/     unverified-AI-output workspace: should fail
tests/               CodeCrew's own suite (41 tests)
```

Evidence for every claim is written to
`<workspace>/.codecrew/evidence/<run-id>/`, and a machine-readable report to
`<workspace>/.codecrew/reports/<run-id>.json`. From Phase 1 on, an agent may
only assert a gate outcome by citing one of those files.

## Roadmap

| Phase | Deliverable | State |
|---|---|---|
| 0 | Sandbox + gate layer + CLI, no LLM | **done** |
| 1 | Single Coder agent, typed contracts, free-tier LLM layer — the measured single-agent baseline | next |
| 2 | Spec Agent + Test-Author with context isolation; prove the mutation score beats coder-written tests | |
| 3 | Diagnostician + Patcher, escalation ladder, full gate re-run per patch | |
| 4 | Architect, multi-file projects, dependency sanity check | |
| 5 | Streamlit UI, Verification Report, trust badges, CI export | |
| 6 | Full evaluation + four-way ablation study | |

The LLM layer is provider-agnostic with free tiers as the default (Groq,
Gemini, or local Ollama), so no phase requires paid API access.

## Running CodeCrew's own tests

```bash
python -m pytest tests -q
```
