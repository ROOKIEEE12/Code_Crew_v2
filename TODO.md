# TODO

Derived from the code, the test suite and the measured results in
`README.md`. Each open item is something that is actually missing or
unmeasured today; nothing here is speculative.

## Done

- [x] **Phase 0** sandbox (Docker + local venv), gates G1-G6, mutation engine,
      verification report, CLI
- [x] **Phase 1** provider-agnostic free-tier LLM layer, typed contracts with
      repair-in-place, tracing, single-agent baseline, benchmark and the
      false-confidence metric
- [x] **Phase 2** spec agent, approval checkpoint, isolated implementer and
      test author, enforced `allowed_context`, side-by-side `compare`
- [x] **Phase 3** diagnosis (code / test / spec defect), guarded patches,
      accept-or-revert on evidence, G7 spec conformance, `--existing` repair
- [x] **Phase 4** architect, multi-module builds, `uses` interface hand-off,
      G8 dependency check, per-module repair, `wordstats` project task
- [x] Live Groq run of single and multi on all 10 tasks; first live
      reconciliation fix (`password_policy`)
- [x] **Phase 5** Next.js web UI (spec review, live gate board, activity
      log), local API (`codecrew serve`), per-file trust badges, USAGE.md
      from the spec, CI export, `codecrew` console script
- [x] Verdict discloses an auto-approved spec with open assumptions
- [x] **Production readiness**: verify a developer's own repository (src
      layout, declared dependencies in a cached isolated env,
      `[tool.codecrew]`, quick/full profiles, `--strict`, SARIF), mutation
      testing on a scratch copy, `codecrew init`, pre-commit hooks, a
      release wheel that carries the UI, and a project-centred web UI with
      history, trends, findings and fix prompts

## Measurement (results that are still missing)

- [ ] Finish the live repair run on `safe_path` (blocked by the Groq daily
      token quota) and record both outcomes in the README
- [ ] Run `--mode project --tasks wordstats` against a live model; Phase 4 has
      only been verified with scripted models
- [x] **Phase 6 harness**: `codecrew eval`, resumable across quota resets,
      architecture x judge ablation, Wilson intervals, exact McNemar
- [x] Four harder tasks (semver, csv_line, expr_eval, token_bucket), each
      validated against a reference and a known-wrong answer
- [ ] Complete the live study: all 14 tasks x 3 conditions x 3 reps
      (`codecrew eval --name main --reps 3`, resumes where it stopped)
- [ ] Live-run a non-Groq provider (Gemini or Ollama); only request shapes
      are tested offline

## Benchmark integrity

- [x] `intervals`: requirement amended to say lists are accepted (disclosed
      in README); its earlier results are not comparable

## Product (after Phase 5)

- [ ] Languages other than Python
- [ ] Generate a module *into* an existing repository (AI builds go to their
      own workspace today)
- [ ] The exported CI workflow has not yet run on GitHub Actions itself;
      run it once on a real repository and record the result
- [ ] Jobs live in server memory; a restart loses in-flight jobs (finished
      workspaces stay on disk and are listed)
- [ ] Browser end-to-end test of the UI (today: logic unit tests, typecheck,
      static build, and a manual headless-Edge render)

## Hardening

- [ ] The Docker sandbox backend has never been executed on this machine
      (no Docker installed); verify it end to end on a machine that has it
- [ ] The Docker backend does not install project dependencies yet (the
      local backend does)
- [ ] A separately authored cross-module integration suite for project mode
- [ ] Unit tests for the interactive approvers (`_interactive_approver`,
      `_interactive_plan_approver`); they are exercised only by hand
- [ ] Per-agent accuracy evals from the original plan (spec checklist scoring,
      test-author catch rate on known-buggy programs, diagnostician accuracy)
- [ ] The 7 old ruff style findings (3x E731, 2x F541, E402, F401), as one
      separate cleanup commit

## Owner actions (not for the agent)

- [ ] Rotate the Groq API key that was pasted into a chat transcript
- [ ] Merge the open branches into `main` (feat/phase5-product, then
      feat/production-ready), so installs from GitHub get this code
- [ ] Push `ci/github-workflows` with a token that has the `workflow` scope
      (CI and release workflows), then tag `v0.2.0` to publish a wheel
- [ ] Choose a license; none is declared
