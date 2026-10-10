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

## Measurement (results that are still missing)

- [ ] Finish the live repair run on `safe_path` (blocked by the Groq daily
      token quota) and record both outcomes in the README
- [ ] Run `--mode project --tasks wordstats` against a live model; Phase 4 has
      only been verified with scripted models
- [ ] Repeat each condition 3-5 times: one run per task cannot rank anything
      (`sql_builder` passed and failed its hidden tests across identical runs)
- [ ] Add harder tasks: single and multi both reach 10/10 hidden, so the suite
      cannot separate them on correctness
- [ ] Live-run a non-Groq provider (Gemini or Ollama); only request shapes
      are tested offline

## Benchmark integrity

- [ ] `intervals`: the hidden test requires list intervals to be accepted but
      the requirement never says so. Amend the requirement (disclosed) and
      re-run only that task under every condition

## Product (after Phase 5)

- [ ] The exported CI workflow has not yet run on GitHub Actions itself;
      run it once on a real repository and record the result
- [ ] Jobs live in server memory; a restart loses in-flight jobs (finished
      workspaces stay on disk and are listed)
- [ ] Browser end-to-end test of the UI (today: logic unit tests, typecheck,
      static build, and a manual headless-Edge render)

## Hardening

- [ ] The Docker sandbox backend has never been executed on this machine
      (no Docker installed); verify it end to end on a machine that has it
- [ ] Install declared third-party dependencies into the sandbox so a project
      that really needs one can run its tests (G8 vets them, nothing installs)
- [ ] A separately authored cross-module integration suite for project mode
- [ ] Unit tests for the interactive approvers (`_interactive_approver`,
      `_interactive_plan_approver`); they are exercised only by hand
- [ ] Per-agent accuracy evals from the original plan (spec checklist scoring,
      test-author catch rate on known-buggy programs, diagnostician accuracy)
- [ ] The 7 old ruff style findings (3x E731, 2x F541, E402, F401), as one
      separate cleanup commit

## Owner actions (not for the agent)

- [ ] Rotate the Groq API key that was pasted into a chat transcript
- [ ] Push `chore/auto-commit-workflow` and open a PR into `main`
