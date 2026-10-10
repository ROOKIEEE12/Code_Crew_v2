"use client";

import { useState } from "react";

import { api, type PendingApproval, type Plan, type Spec } from "@/lib/api";

export function SpecView({ spec }: { spec: Spec }) {
  return (
    <div className="space-y-4">
      <div>
        <p className="font-mono text-sm text-muted">{spec.module}.py</p>
        <p className="mt-1">{spec.summary}</p>
      </div>

      {spec.assumptions.length > 0 ? (
        <div className="rounded-lg border border-warn/40 bg-warn-soft p-3">
          <h4 className="text-sm font-semibold text-warn">
            Assumptions: check these ({spec.assumptions.length})
          </h4>
          <p className="mb-2 text-xs text-warn">
            Each is a place your requirement was silent and the model had to guess. A wrong guess here
            becomes a perfectly tested wrong program.
          </p>
          <ul className="list-disc space-y-1 pl-5 text-sm">
            {spec.assumptions.map((a) => (
              <li key={a}>{a}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="space-y-3">
        {spec.functions.map((fn) => (
          <div key={fn.signature} className="rounded-lg border border-line p-3">
            <code className="block overflow-x-auto font-mono text-sm font-semibold">{fn.signature}</code>
            <p className="mt-1 text-sm text-muted">{fn.description}</p>
            {fn.raises.length > 0 ? (
              <p className="mt-1 text-xs text-muted">raises: {fn.raises.join(", ")}</p>
            ) : null}
            {fn.examples.length > 0 ? (
              <table className="mt-2 w-full text-xs">
                <tbody>
                  {fn.examples.map((ex) => (
                    <tr key={ex.call} className="border-t border-line">
                      <td className="py-1 pr-3 font-mono">{ex.call}</td>
                      <td className="py-1 font-mono text-muted">
                        {ex.raises ? `raises ${ex.raises}` : `→ ${ex.result}`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : null}
          </div>
        ))}
      </div>

      <List title="Uses (already-built modules)" items={spec.uses} />
      <List title="Constraints" items={spec.constraints} />
      <List title="Security" items={spec.security} />
      <List title="Acceptance criteria" items={spec.acceptance_criteria} />
    </div>
  );
}

export function PlanView({ plan }: { plan: Plan }) {
  return (
    <div className="space-y-4">
      <p>{plan.summary}</p>
      <ol className="space-y-2">
        {plan.modules.map((m) => (
          <li key={m.name} className="rounded-lg border border-line p-3">
            <div className="flex flex-wrap items-baseline gap-2">
              <code className="font-mono font-semibold">{m.name}.py</code>
              {m.depends_on.length > 0 ? (
                <span className="text-xs text-muted">uses {m.depends_on.join(", ")}</span>
              ) : null}
            </div>
            <p className="mt-1 text-sm text-muted">{m.responsibility}</p>
          </li>
        ))}
      </ol>
      {plan.dependencies.length > 0 ? (
        <div className="rounded-lg border border-warn/40 bg-warn-soft p-3 text-sm">
          <h4 className="font-semibold text-warn">Third-party packages</h4>
          <p className="mb-1 text-xs text-warn">Every one is supply-chain surface. G8 checks they exist.</p>
          <ul className="list-disc pl-5">
            {plan.dependencies.map((d) => (
              <li key={d.name}>
                <code className="font-mono">{d.name}</code>: {d.purpose}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <List title="Notes" items={plan.notes} />
    </div>
  );
}

function List({ title, items }: { title: string; items: string[] }) {
  if (!items || items.length === 0) return null;
  return (
    <div>
      <h4 className="text-sm font-semibold">{title}</h4>
      <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm text-muted">
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

export function ApprovalPanel({
  jobId,
  pending,
  onDecided,
}: {
  jobId: string;
  pending: PendingApproval;
  onDecided: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(() => JSON.stringify(pending.payload, null, 2));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function decide(approve: boolean) {
    setBusy(true);
    setError(null);
    try {
      let edited: Spec | Plan | undefined;
      if (approve && editing) {
        try {
          edited = JSON.parse(draft) as Spec | Plan;
        } catch {
          throw new Error("That is not valid JSON.");
        }
      }
      await api.decide(jobId, approve, edited);
      onDecided();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const what = pending.type === "plan" ? "project plan" : "specification";
  return (
    <section className="card border-2 border-accent p-5" aria-live="polite">
      <div className="mb-1 flex flex-wrap items-baseline gap-2">
        <h2 className="text-lg font-semibold">Review the {what}</h2>
        {pending.module ? <span className="font-mono text-sm text-muted">{pending.module}</span> : null}
      </div>
      <p className="mb-4 text-sm text-muted">
        No code exists yet. The implementer and the test author will each receive{" "}
        <strong>only this {what}</strong>, never each other&apos;s work, so a mistake here is the one
        mistake the gates cannot catch.
      </p>

      {editing ? (
        <textarea
          className="field h-96 font-mono text-xs"
          spellCheck={false}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          aria-label={`Edit the ${what} as JSON`}
        />
      ) : pending.type === "plan" ? (
        <PlanView plan={pending.payload as Plan} />
      ) : (
        <SpecView spec={pending.payload as Spec} />
      )}

      {error ? <p className="mt-3 rounded-md bg-bad-soft px-3 py-2 text-sm text-bad">{error}</p> : null}

      <div className="mt-5 flex flex-wrap gap-2">
        <button className="btn btn-ok" disabled={busy} onClick={() => decide(true)}>
          {editing ? "Approve my edited version" : "Approve and build"}
        </button>
        <button className="btn" disabled={busy} onClick={() => setEditing((v) => !v)}>
          {editing ? "Back to the readable view" : "Edit as JSON"}
        </button>
        <button className="btn ml-auto text-bad" disabled={busy} onClick={() => decide(false)}>
          Reject
        </button>
      </div>
    </section>
  );
}
