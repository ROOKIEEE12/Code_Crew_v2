"use client";

import { useEffect, useState } from "react";

import { api, type ConditionSummary, type Evaluation } from "@/lib/api";

const pct = (v: number | null) => (v === null ? "n/a" : `${Math.round(v * 100)}%`);
const ci = (v: [number, number] | null) => (v ? `${pct(v[0])}–${pct(v[1])}` : "");

const LABEL: Record<string, string> = {
  single: "Single agent",
  multi: "Multi-agent",
  multi_repair: "Multi-agent + repair",
};

export default function EvaluationPage() {
  const [evals, setEvals] = useState<Evaluation[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .evaluations()
      .then((r) => setEvals(r.evaluations))
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  return (
    <div className="space-y-6">
      <section className="max-w-3xl">
        <h1 className="text-2xl font-bold">Evaluation</h1>
        <p className="mt-2 text-sm text-muted">
          The headline number is <strong>false confidence</strong>: the pipeline said the code was fine and hidden
          tests it never saw say it is not. Each architecture is judged twice from the same runs: by the full gate
          layer, and by &quot;its own tests pass&quot; alone (what most AI tools check). Intervals are 95% Wilson
          intervals; with this few runs they are wide, and overlapping intervals mean the data cannot rank the
          conditions.
        </p>
      </section>

      {error ? <p className="rounded-md bg-bad-soft px-3 py-2 text-sm text-bad">{error}</p> : null}
      {evals && evals.length === 0 ? (
        <div className="card p-4 text-sm text-muted">
          No study yet. Run one (resumable, so it can continue across free-tier quota resets):
          <pre className="code mt-2 px-3 text-fg">codecrew eval --name main --reps 3</pre>
        </div>
      ) : null}

      {evals?.map((ev) => (
        <section key={ev.path} className="card overflow-x-auto p-4">
          <div className="mb-3 flex flex-wrap items-baseline gap-3">
            <h2 className="font-mono font-semibold">{ev.name}</h2>
            <span className="text-xs text-muted">
              {ev.completed} run(s) complete{ev.pending ? `, ${ev.pending} still to run` : ""}
            </span>
          </div>
          <table className="w-full min-w-[720px] text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-muted">
                <th className="py-2 pr-3">Architecture</th>
                <th className="pr-3">Judged by</th>
                <th className="pr-3">Runs</th>
                <th className="pr-3">Claimed OK</th>
                <th className="pr-3">False confidence</th>
                <th className="pr-3">Hidden tests pass</th>
                <th className="pr-3">Mutation</th>
                <th>Tokens / run</th>
              </tr>
            </thead>
            <tbody>
              {ev.conditions.map((c: ConditionSummary) => (
                <tr key={`${c.condition}-${c.judge}`} className="border-b border-line/60">
                  <td className="py-2 pr-3 font-medium">{LABEL[c.condition] ?? c.condition}</td>
                  <td className="pr-3 text-muted">{c.judge === "full" ? "all gates" : "own tests only"}</td>
                  <td className="pr-3">{c.runs}</td>
                  <td className="pr-3">{c.claimed_ok}</td>
                  <td className="pr-3">
                    <span className={c.false_confidence > 0 ? "font-semibold text-bad" : "font-semibold text-ok"}>
                      {c.false_confidence} ({pct(c.false_confidence_rate)})
                    </span>{" "}
                    <span className="text-xs text-muted">{ci(c.fc_ci)}</span>
                  </td>
                  <td className="pr-3">
                    {pct(c.hidden_pass_rate)} <span className="text-xs text-muted">{ci(c.hp_ci)}</span>
                  </td>
                  <td className="pr-3">{c.mean_mutation_score === null ? "n/a" : `${c.mean_mutation_score.toFixed(0)}%`}</td>
                  <td>{c.mean_tokens === null ? "n/a" : Math.round(c.mean_tokens).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-2">
            <div>
              <h3 className="mb-1 text-sm font-semibold">What the deep gates add</h3>
              <p className="mb-2 text-xs text-muted">
                Same runs, two judges: cases where &quot;its own tests pass&quot; would have shipped wrong code.
              </p>
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-muted">
                    <th className="py-1 pr-2">Architecture</th>
                    <th className="pr-2">Wrong, own tests green</th>
                    <th className="pr-2">Caught by gates</th>
                    <th>Correct, rejected</th>
                  </tr>
                </thead>
                <tbody>
                  {ev.gate_value.map((g) => (
                    <tr key={g.condition} className="border-b border-line/60">
                      <td className="py-1 pr-2">{LABEL[g.condition] ?? g.condition}</td>
                      <td className="pr-2">{g.own_tests_false_confidence}</td>
                      <td className="pr-2 text-ok">{g.caught_by_full_gates}</td>
                      <td className="text-warn">{g.extra_rejections_of_correct_code}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div>
              <h3 className="mb-1 text-sm font-semibold">Paired comparison (exact McNemar)</h3>
              <p className="mb-2 text-xs text-muted">Same task and repetition; only the discordant pairs count.</p>
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-muted">
                    <th className="py-1 pr-2">A vs B</th>
                    <th className="pr-2">Pairs</th>
                    <th className="pr-2">Only A</th>
                    <th className="pr-2">Only B</th>
                    <th>p</th>
                  </tr>
                </thead>
                <tbody>
                  {ev.paired.map((p) => (
                    <tr key={`${p.a}-${p.b}`} className="border-b border-line/60">
                      <td className="py-1 pr-2">
                        {p.a} vs {p.b}
                      </td>
                      <td className="pr-2">{p.pairs}</td>
                      <td className="pr-2">{p.a_only_pass}</td>
                      <td className="pr-2">{p.b_only_pass}</td>
                      <td>{p.p_value.toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <details className="mt-6">
            <summary className="cursor-pointer text-sm font-semibold">Per task</summary>
            <table className="mt-2 w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-muted">
                  <th className="py-1 pr-2">Task</th>
                  {(ev.meta.conditions ?? []).map((c) => (
                    <th key={c} className="pr-2">
                      {LABEL[c] ?? c}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {Object.entries(ev.per_task).map(([task, cells]) => (
                  <tr key={task} className="border-b border-line/60">
                    <td className="py-1 pr-2 font-mono text-xs">{task}</td>
                    {(ev.meta.conditions ?? []).map((c) => (
                      <td key={c} className={`pr-2 ${cells[c]?.includes("FC") ? "text-bad" : ""}`}>
                        {cells[c] ?? "–"}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </section>
      ))}
    </div>
  );
}
