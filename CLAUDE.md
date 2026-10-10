# CodeCrew: working agreement

CodeCrew is a Python verification pipeline that happens to use LLMs to write
code. The one design rule that everything else follows from: **no agent may
declare its own success.** Every "pass" comes from a deterministic gate (a
compiler, a linter, a real sandboxed test run, a mutation score), and the gate
layer contains no LLM call. Do not weaken that to make something easier.

## Commands

Interpreter: use the shared tool venv, which has pytest, ruff, mypy and
hypothesis. The system Python has only pytest.

```bash
PY="$HOME/.codecrew/venv/Scripts/python.exe"      # Windows; bin/python elsewhere
```

| Purpose | Command |
|---|---|
| Compile check | `"$PY" -m compileall -q codecrew tests` |
| Lint (what blocks a commit) | `"$PY" -m ruff check codecrew tests --select E9,F63,F7,F82` |
| Fast tests (~35 s) | `"$PY" -m pytest tests -q -x -m "not slow" -p no:cacheprovider` |
| Full tests (~3 min) | `"$PY" -m pytest tests -q -p no:cacheprovider` |
| Full lint (advisory) | `"$PY" -m ruff check codecrew tests` |
| Verify a workspace | `python -m codecrew.gates run <dir>` |
| Provider status | `python -m codecrew doctor` |

There is no build step and no package manager beyond pip/`pyproject.toml`.
The full lint currently reports a few old style findings (unused import,
lambda assignment); those are not defects and do not block a commit. Do not
"fix" them as a drive-by inside an unrelated change.

## Git workflow

- Work in **small, complete, meaningful changes**: one logical change per
  commit, leaving the tests green.
- **Never work on `main`/`master`.** If you are on one, first
  `git switch -c <type>/<short-topic>` (for example `feat/spec-badge`).
- Before committing, the checks above (compile, lint, fast tests) must pass.
  Run the full test suite as well when you touched gates, pipeline, repair or
  agents.
- **Commit only after checks pass**, with a conventional commit message:
  `feat|fix|docs|test|refactor|perf|build|ci|chore(scope): summary`, subject
  under 100 characters, imperative mood, body explaining *why*.
- Write the message to `.claude/commit-message.txt` before you stop; the
  Stop hook uses it. Without it the hook falls back to a generic message.
- **Push to the current branch** after each successful commit
  (`git push origin HEAD`). The Stop hook does this.
- **Never**: force-push; amend or rewrite published history; create empty,
  backdated or activity-padding commits; use `--no-verify`.
- **Never commit**: `.env*` (except `.env.example`), secrets or API keys,
  private keys, `venv/`, `bench_runs/`, `.codecrew/`, `__pycache__/`, caches,
  or any generated output.
- **Commit identity** is the repository's configured
  `ROOKIEEE12 <168962806+ROOKIEEE12@users.noreply.github.com>`. Do not add
  `Co-Authored-By` trailers or any other attribution line to commits.
- If a task is ambiguous, destructive, or needs credentials, **stop and ask**.

The Stop hook (`.claude/hooks/auto-commit-push.sh`) enforces the mechanical
parts: it refuses on `main`, refuses secrets and generated files, runs the
checks, and never force-pushes. Disable it with
`touch .claude/auto-commit.disabled`.

## Project rules that are easy to break

- **The gate layer is LLM-free.** Never call a model from `codecrew/gates/`.
- **A skipped gate is never a pass.** `SKIP` downgrades the verdict; "does
  not apply" (`Gate.applicable`) is the only way to leave a gate out.
- **Isolation is enforced, not conventional.** Agents declare
  `allowed_context`; the Test-Author must never receive implementation code.
  Do not widen an agent's context to make a prompt easier to write.
- **A patch is kept only on evidence of progress**, and every gate re-runs
  after every patch. Do not add shortcuts that skip re-verification.
- **Benchmark integrity.** The tasks, their hidden tests and the gate
  calibration are what the reported numbers mean. If you change one, say so
  in the commit message and in the README, because earlier numbers are then
  no longer comparable.
- **Never put a realistic vendor-prefixed key in a sample.** GitHub push
  protection rejects the whole push (it already happened once).
- Report outcomes faithfully: a failing test or a negative result is written
  down as such, not softened.

## Where things are

`codecrew/gates/` deterministic checks (G1-G8) and the runner;
`codecrew/agents/` typed-contract agents; `codecrew/pipeline.py`
single/multi/project builds; `codecrew/repair.py` the reconciliation loop;
`codecrew/bench/` tasks with hidden tests and the false-confidence metric;
`tests/` the suite; `samples/` one passing and one deliberately bad workspace.
`README.md` has the measured results and the known limitations; keep it
honest. `TODO.md` is the task list.
