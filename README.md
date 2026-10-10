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

## Status: Phases 0 and 1 complete

- **Phase 0** — the gate layer, runnable standalone with no LLM and no API key.
  This ordering is deliberate: the judge is built and validated before
  anything is judged.
- **Phase 1** — the free-tier LLM layer, typed agent contracts, and the
  single-agent baseline wired to the gates. This is the baseline that the
  multi-agent architecture must beat.

**No paid API is required at any point.** Providers are interchangeable and
free tiers are the default: Groq, Gemini, local Ollama, or the fully offline
`scripted` provider.

## Quick start

```bash
# One-time: build the shared tool environment (~1-2 min)
python -c "from pathlib import Path; from codecrew.sandbox.local import LocalSandbox; LocalSandbox(Path('.')).ensure()"

# What's usable right now?
python -m codecrew doctor

# Verify an existing workspace (no LLM involved)
python -m codecrew.gates run samples/strength_good    # -> VERIFIED,   exit 0
python -m codecrew.gates run samples/strength_vibe    # -> UNVERIFIED, exit 1

# Generate and verify, fully offline via a replay file
python -m codecrew build "Build an in-memory URL shortener" \
  --out /tmp/shortener --provider scripted \
  --script samples/replays/url_shortener.json --clean

# Generate and verify with a real free-tier model
export GROQ_API_KEY=...      # or GEMINI_API_KEY, or run Ollama
python -m codecrew build "Build a CSV column summariser" --out /tmp/csvtool
```

Gate flags: `--only G1,G2`, `--disable G4`, `--sandbox docker`,
`--min-coverage 85`, `--min-mutation 70`, `--max-mutants 100`, `--json out.json`,
`--quiet`.
Build flags: `--provider`, `--script`, `--fast-model`, `--strong-model`,
`--temperature`, `--clean`, `--no-verify`.

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

**Equivalent mutants are filtered.** Docstrings, type annotations and
exception *message* strings are never mutated: blanking `raise
ValueError("too small")` changes no behaviour, so a suite that correctly
asserts on the exception type would be penalised for a mutant it should not
catch. Excluding them raised the demo workspace's score from 81.5% to 91.7%
with no change to the tests — the earlier number was measuring the metric's
own noise. The logic *guarding* a raise is still mutated.

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

## Phase 1: agents and the LLM layer

### Provider-agnostic, free by default

`python -m codecrew doctor` reports what is usable. `auto` walks Groq →
Gemini → Ollama and picks the first available; **Anthropic is deliberately
excluded from `auto`**, because a default that silently spends money is the
wrong default. Requests go out over `urllib`, so there is no third-party HTTP
dependency.

Calls are routed by task difficulty ([`llm/client.py`](codecrew/llm/client.py)):
mechanical work (`FORMAT`) goes to the cheap model, reasoning (`GENERATE`,
`DIAGNOSE`, `PATCH`) to the strongest available. Temperature is 0 by default.
Rate limits are treated as the normal path, not an error path — free tiers
throttle hard, so retries use exponential backoff with jitter.

### Typed contracts, repaired in place

A large share of multi-agent failures are hand-off failures, not reasoning
failures. Every agent's output is validated at the boundary
([`agents/contracts.py`](codecrew/agents/contracts.py)) and a violation is
retried **with the specific error appended**, so the model is told what was
wrong rather than asked again and hoped at. Rejected: malformed JSON, missing
fields, duplicate paths, oversized files, absolute paths, `..` escapes,
unsupported extensions, and code that does not parse. That last one catches
truncated responses — the most common real failure — for the price of an
`ast.parse` rather than a full gate run.

### The no-self-grading rule, structurally

The agent is asked for a `self_assessment`, and it is printed under an
explicit heading:

```
UNVERIFIED CLAIM (the agent's own opinion, not a result)
  I am least confident about URL normalisation: only surrounding whitespace
  is stripped, so http://example.com and http://example.com/ are treated as
  different URLs.
```

It is kept because it is genuinely useful, and it is quarantined because it
comes from the thing being judged. The verdict is computed only from gate
results.

### Offline, reproducible runs

`--provider scripted --script <file.json>` replays canned responses, so the
full pipeline is demonstrable with no key, no quota and no network — and the
evaluation can be replayed deterministically later.

## Layout

```
codecrew/
  config.py          thresholds and gate toggles (all tunable for ablations)
  results.py         Status/Severity/Finding/GateResult/GateReport + verdict policy
  mutation.py        the AST mutation engine
  report.py          verification report + run report
  trace.py           JSONL tracing: every call, retry, gate and cost
  pipeline.py        Phase 1 orchestration: requirement -> code -> gates
  __main__.py        CLI: doctor | build | verify
  sandbox/           base contract, docker backend, local-venv backend
  llm/
    base.py          Provider contract, urllib transport, error taxonomy
    providers.py     groq, gemini, ollama, anthropic, scripted
    client.py        task routing, retries/backoff, tracing, replay
  agents/
    base.py          Agent contract + the contract-repair loop
    contracts.py     typed output validation with model-readable feedback
    coder.py         the Coder agent / single-agent baseline
  gates/
    base.py          Gate contract, workspace discovery, evidence capture
    syntax.py        G1      tests.py     G2      coverage.py  G3
    security.py      G5      mutation.py  G4      properties.py G6
    runner.py        sequencing, dependency skips, aggregation
    __main__.py      CLI
samples/
  strength_good/     reference workspace: should pass
  strength_vibe/     unverified-AI-output workspace: should fail
  replays/           canned responses for offline demos
tests/               CodeCrew's own suite (105 tests)
```

Evidence for every claim is written to
`<workspace>/.codecrew/evidence/<run-id>/`, and a machine-readable report to
`<workspace>/.codecrew/reports/<run-id>.json`. From Phase 1 on, an agent may
only assert a gate outcome by citing one of those files.

## Roadmap

| Phase | Deliverable | State |
|---|---|---|
| 0 | Sandbox + gate layer + CLI, no LLM | **done** |
| 1 | Free-tier LLM layer, typed contracts, Coder agent, single-agent baseline | **done** |
| 2 | Spec Agent + Test-Author with context isolation; prove the mutation score beats coder-written tests | next |
| 3 | Diagnostician + Patcher, escalation ladder, full gate re-run per patch | |
| 4 | Architect, multi-file projects, dependency sanity check | |
| 5 | Streamlit UI, trust badges, CI export | |
| 6 | Full evaluation + four-way ablation study | |

The LLM layer is provider-agnostic with free tiers as the default (Groq,
Gemini, or local Ollama), so no phase requires paid API access.

## Known limitations

Stated plainly, because the project's whole argument is about not overstating
verification:

- **Without Docker, isolation is weak.** The `local-venv` backend separates
  dependencies but not the host; generated code can reach the network and the
  filesystem. The report says so on every run rather than implying safety it
  does not provide.
- **Mutation testing samples.** Above `max_mutants` (default 40) sites are
  sampled with a fixed seed, so the score is an estimate — comparable between
  runs, but not exhaustive.
- **G6 measures reachability, not quality.** It checks that a property test
  references a function, not that the property asserted is meaningful. It is
  advisory for that reason.
- **No debug loop yet.** Phase 1 generates once and reports. A failing verdict
  is not yet repaired automatically; that is Phase 3.

## Running CodeCrew's own tests

```bash
python -m pytest tests -q
```
