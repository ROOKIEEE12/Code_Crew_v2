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
| G7 | Spec conformance | yes | the spec's own examples, executed against the code (multi/project only) |
| G8 | Dependency check | yes | undeclared imports (offline); existence, vulnerabilities, age (PyPI) |

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

### The benchmark and the false-confidence metric

`python -m codecrew bench` runs 10 self-authored tasks (4 easy, 4 medium,
2 security: path traversal and SQL injection) and scores each result against
**hidden tests** the agent never sees. The headline number is not accuracy
but **false confidence**: the pipeline said the code was fine, and the hidden
tests say it was not.

| pipeline claimed | hidden tests | outcome |
|---|---|---|
| ok | pass | `true_pass` |
| ok | fail | **`FALSE_CONFIDENCE`** — the failure this project exists to minimise |
| not ok | fail | `caught` — the gates worked |
| not ok | pass | `false_alarm` — too strict |

The rate's denominator is *claims of success*, not tasks, and it is reported
as undefined rather than 0% when nothing was claimed ok.

The benchmark validates itself: every task ships a reference solution, and
the test suite asserts all 10 pass their hidden tests (satisfiable) while an
empty module fails them (able to say no). Without that, a low score could
just mean a broken test.

```bash
python -m codecrew bench --list
python -m codecrew bench --provider groq --out bench_runs/baseline
python -m codecrew bench --disable G4 ...     # faster, weaker verdict
```

### Free-tier model names churn

Hosted free-tier models are retired often: Groq retired
`llama-3.3-70b-versatile` on 2026-08-16 and Google deprecated Gemini 2.0 Flash
in mid-2026, which would have broken the first call. Defaults were updated
(last checked 2026-10-10), but treat them as best-effort and override with no
code change:

```bash
export CODECREW_MODEL_STRONG=...   CODECREW_MODEL_FAST=...
```

A 404 is reported as non-retryable with this hint, rather than retried four
times with backoff.

## Phase 2: the multi-agent pipeline

```
requirement -> [Spec agent] -> spec.json -> HUMAN APPROVAL
                                   |
                  +----------------+----------------+
                  v                                 v
          [Implementer]                      [Test-Author]
          sees: spec only                    sees: spec only
                  \                                 /
                   +---------> same gate layer <---+
```

`python -m codecrew build "<requirement>" --out DIR --mode multi` (the default)
runs it; `--mode single` runs the one-agent baseline through identical gates.

### Isolation is a property, not a convention

Each agent declares `allowed_context`, and the base class raises
`ContextViolation` if it is handed anything else — or is missing something it
needs — *before any model call is made*. The Test-Author cannot be given the
implementation; the Implementer cannot be given the tests or the raw
requirement. The pipeline also writes an `agent_context` event per agent, so
what each one was shown is auditable from the trace afterwards, and the test
suite checks the actual prompts that reached the model for marker strings.

Contracts reinforce it. The Implementer's output is **rejected** if it
contains a test file (otherwise it would quietly reintroduce self-grading).
The Test-Author's output is rejected if it contains non-test code, never
imports the module under test, defines no `test_*` function, or contains no
assertion — a test that cannot fail verifies nothing.

### The approval checkpoint

The spec is shown with its **assumptions** called out: each is a place the
requirement was silent and the model had to guess. A person reviews roughly
twenty lines here instead of five hundred lines of code later. Options at the
prompt: approve, reject, or *edit* (writes `spec.draft.json`; edit it and
re-run with `--spec`). `--approve auto` skips the prompt for scripted use.
The spec is JSON rather than YAML so the project stays dependency-free.

### What is and is not claimed

Phase 2 builds the apparatus for the project's central hypothesis — that tests
written without sight of the code detect more injected bugs than tests the
implementer wrote about its own work. **It does not assert the hypothesis.**
`python -m codecrew compare A/results.json B/results.json` puts the two modes
side by side on hidden-test pass rate, mutation score, branch coverage and
false-confidence rate. With 10 tasks and one run each, differences of a single
task are within noise, and the output says so.

A disagreement between Implementer and Test-Author (own tests fail, G2) is
reported as such rather than hidden: it means the spec was ambiguous or one
of them was wrong, and it is the most informative failure in the pipeline.

### Measured result (10 tasks, one run each, Groq `openai/gpt-oss-120b`)

| | single | multi |
|---|---|---|
| hidden tests passed | 10/10 | 10/10 |
| false confidence | 0 of 8 claims | 0 of 4 claims |
| own tests green | 8/10 | **4/10** |
| false alarms (correct code, rejected) | 2 | **6** |
| mean mutation score | 88.9% (8 tasks) | 93.8% (4 tasks) |
| tokens | 29,056 | 72,452 |

**The hypothesis is not supported at this sample size.** The mutation means are
over *different* task sets (G4 runs only when a workspace's own tests pass), so
they are not comparable; on the 4 tasks both modes scored it is 92.3% vs 93.8%,
which is noise. What the data does show is a cost: independent test authoring
failed on 6 of 10 tasks whose implementations were all hidden-test correct, at
2.5x the tokens. Both modes saturate the hidden tests (10/10), so this
benchmark cannot yet separate them on correctness.

Why the 6 multi-mode workspaces failed their own tests (all 10 implementations
were correct, so these were *test or spec* defects):

| cause | tasks |
|---|---|
| Test asserts the module's public surface, which no spec states | clamp, roman, rpn |
| Test invents behaviour the spec leaves open (a `TypeError`) | intervals |
| Property test with a bug of its own (divisor of 0) | rpn |
| Environment-dependent test (symlinks need privilege on Windows) | safe_path |
| **The spec's own example was wrong** (`'short'` listed `no_lower`) | password_policy |

The last row is the approval checkpoint's case for existing: the benchmark
auto-approves, a person reading that example would have caught it. It also
shows that the bottleneck of independence is reconciliation, not generation:
two agents that cannot see each other need an arbiter when they disagree, and
the only legitimate arbiter is the spec.

Run-to-run variance is real: the SQL task failed its hidden tests in one run
(a `$` regex anchor that accepts a trailing newline) and passed in the next, same
model, same prompt, temperature 0. One run per task cannot support a ranking.

### Gate calibration: a finding from the benchmark

The first live baseline rejected a *correct* palindrome solution as
`UNVERIFIED` because of one unused `typing.Iterable` import. Worse, because G1
gates the tests, its tests never ran, so a correct answer received no signal
at all. Pure hygiene rules (`F401`, `F841`, `F403`, `F405`) are now reported
but no longer block; real defects (undefined names, redefinitions, broken
formatting) still do. A gate that blocks on style teaches people to ignore it.

### Offline, reproducible runs

`--provider scripted --script <file.json>` replays canned responses, so the
full pipeline is demonstrable with no key, no quota and no network — and the
evaluation can be replayed deterministically later.

## Phase 3: reconciliation

When the gates fail, the question is not "how do I make the tests pass" but
**"which artefact is wrong?"** A loop that only knows the first question
bends correct code to satisfy a wrong test. Phase 2 measured exactly that
risk: 6 of 10 workspaces failed their own tests while every implementation
was correct.

```
gates fail -> [Diagnostician]  sees: spec + code + tests + failure evidence
                 |  judged against the spec's rule text
                 +- code_defect -> patch the implementation
                 +- test_defect -> patch the tests
                 +- spec_defect -> correct the spec (human sees it),
                                   re-derive tests from it, code untouched
              -> every gate re-runs -> accept or revert
```

- **A patch is kept only on evidence.** More gates must pass *and* none that
  passed before may now fail; otherwise the workspace is restored exactly.
  Counting passing gates (not failing ones) matters: fixing G2 lets G3 run
  for the first time, which can add failures. That is progress being
  unmasked, and a failure-count metric would revert the patches that worked.
- **Guards against the cheap route to green:** a patch may only touch files it
  was given and must return them whole; a test patch may remove at most as
  many tests as failed and never all of them (a test of unspecified behaviour
  has no correct form, so removing exactly that one is the fix); a spec
  repair may fix rules and examples but not rename the API.
- **Short ladder:** patch, patch again with the failed attempts as negative
  examples, then stop and say so. Regeneration is deliberately not a rung:
  at temperature 0 it would mostly reproduce the same output.
- A repaired run reports its **pre-repair verdict** beside the final one.

**G7 spec conformance** is the deterministic half: it executes the spec's own
examples against the implementation inside the sandbox, with no model. It
would have caught the Phase 2 password example (`'short'` listing `no_lower`)
in seconds, and says *which example* disagrees. A mismatch does not say who
is wrong, which is the Diagnostician's job.

`build --repair N` (default 2); `bench --repair N` (default 0, an explicit
ablation); `bench --existing --repair N` applies the loop to workspaces that
already exist, isolating its effect on identical artefacts.

**Tuning caveat, stated plainly:** the Test-Author's contract now rejects
assertions about a module's exported names, and its prompt forbids symlinks
and zero divisors. Those rules came from the Phase 2 failures, so the same 10
tasks are no longer an unbiased measure for those dimensions.

## Phase 4: multi-module projects and dependency checks

`build --mode project` adds an **Architect** that splits a requirement that
is too big for one module into 2-8 modules, each then built by exactly the
same isolated spec / implementer / test-author agents.

- **Plan first, approved first.** Module boundaries are cheapest to fix
  before any spec exists. The plan lists third-party packages with the
  reviewer's attention drawn to them, since each is supply-chain surface.
- **Dependencies-first build, with interfaces set by code, not by a model.**
  A module is handed the *signatures* of the modules it may import, taken
  from their already-built specs and written into its spec's `uses` field by
  the pipeline. A model cannot drift an interface it never retyped.
- **The plan owns names.** If the spec agent renames a module, the planned
  name wins, because other modules already import it by that name. Module
  names that would shadow the standard library (`random.py`) are rejected.
- **The loop works per module.** A failure is attributed to the module its
  findings point at, and the correction is written to that module's spec.

**G8 dependency check**, two tiers kept apart so neither impersonates the
other:

| tier | needs network | catches |
|---|---|---|
| offline, always | no | every third-party import must be declared in the approved plan: a hallucinated dependency fails here; "stdlib only" is *checked*, not assumed |
| online, when PyPI is reachable | yes | declared package must exist (404 is CRITICAL), must have no known vulnerability, and is flagged when brand new or abandoned |

If PyPI cannot be reached the gate reports **SKIP**, not a clean pass. Only
implementation files are scanned; test imports (pytest, hypothesis) belong to
the sandbox toolchain.

One project task is included (`bench --tasks wordstats`), kept out of the
default suite so the single-module numbers stay exactly what they were.

**Limits of Phase 4, stated plainly:**
- Declared third-party packages are vetted but **not installed** in the
  sandbox (that would need network), so a project that genuinely requires one
  cannot run its tests yet. The Implementer is told stdlib-only, and the plan
  is where an exception is argued for.
- Cross-module behaviour is covered by each module's own tests plus the
  spec-example check; there is no separately authored integration suite.
- Project mode is verified offline with scripted models and real gates, and
  has **not** yet been measured against a live model.

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
tests/               CodeCrew's own suite (312 tests)
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
| 2 | Spec Agent + Test-Author with context isolation, approval checkpoint, comparison harness | **done; hypothesis not supported at n=10** |
| 3 | Reconciliation loop: classify a failure as code / test / spec defect against the spec, patch the right artefact, full gate re-run per patch | **built and tested offline; live measurement partial** (Groq daily quota) |
| 4 | Architect, multi-module projects, dependency sanity gate | **built and tested offline; not yet measured live** |
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
- **The baseline number does not exist yet.** The benchmark harness is built
  and validated, but no live model has been run through it, because that
  needs a free API key. Until `bench` is run with a real provider, the
  single-agent baseline is unmeasured and no comparison claim can be made.
- **Live providers are tested by request shape only.** Payloads and response
  parsing are asserted offline; no real endpoint has been called.

## Running CodeCrew's own tests

```bash
python -m pytest tests -q
```
