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
        </section>
      ))}
    </div>
  );
}
