"use client";

import { useMemo, useState } from "react";

import type { FileBadge, GateResult, RunResult } from "@/lib/api";

import { SpecView, PlanView } from "./SpecReview";
import { Pill, VerdictBanner, VerdictPill, toneOf } from "./Verdict";

type Tab = "files" | "gates" | "spec" | "usage" | "repairs" | "ci";

export function ResultView({
  result,
  workflow,
  onLoadWorkflow,
}: {
  result: RunResult;
  workflow?: string | null;
  onLoadWorkflow?: () => void;
}) {
  const tabs: { key: Tab; label: string; show: boolean }[] = [
    { key: "files", label: `Files (${result.files.length})`, show: result.files.length > 0 },
    { key: "gates", label: "Gates & findings", show: !!result.report },
    {
      key: "spec",
      label: result.plan ? "Plan & specs" : "Spec",
      show: !!result.spec || !!result.plan || Object.keys(result.specs).length > 0,
    },
    { key: "usage", label: "USAGE.md", show: !!result.usage },
    { key: "repairs", label: `Repair log (${result.repairs.length})`, show: result.repairs.length > 0 },
    { key: "ci", label: "CI export", show: !!onLoadWorkflow },
  ];
  const visible = tabs.filter((t) => t.show);
  const [tab, setTab] = useState<Tab>(visible[0]?.key ?? "files");

  return (
    <div className="space-y-4">
      <VerdictBanner verdict={result.verdict} note={result.verdict_note} initial={result.initial_verdict} />

      {result.failure ? (
        <div className="card border-bad/40 bg-bad-soft p-4 text-sm text-bad">
          <strong>Stopped:</strong> {result.failure}
        </div>
      ) : null}

      <Facts result={result} />

      {visible.length > 0 ? (
        <div className="card">
          <div role="tablist" className="flex flex-wrap gap-1 border-b border-line px-2 pt-2">
            {visible.map((t) => (
              <button
                key={t.key}
                role="tab"
                aria-selected={tab === t.key}
                onClick={() => {
                  setTab(t.key);
                  if (t.key === "ci" && !workflow) onLoadWorkflow?.();
                }}
                className={`rounded-t-md px-3 py-2 text-sm ${
                  tab === t.key ? "border-b-2 border-accent font-semibold" : "text-muted hover:text-fg"
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
          <div className="p-4">
            {tab === "files" ? <Files result={result} /> : null}
            {tab === "gates" && result.report ? <Gates results={result.report.results} /> : null}
            {tab === "spec" ? <Specs result={result} /> : null}
            {tab === "usage" && result.usage ? <pre className="code px-4 whitespace-pre-wrap">{result.usage}</pre> : null}
            {tab === "repairs" ? <Repairs result={result} /> : null}
            {tab === "ci" ? <Ci workflow={workflow} /> : null}
          </div>
        </div>
      ) : null}

      {result.unverified_claim ? (
        <div className="card p-4">
          <h3 className="text-sm font-semibold">
            Unverified claim <span className="font-normal text-muted">(the agent&apos;s own opinion, not a result)</span>
          </h3>
          <p className="mt-1 text-sm italic text-muted">“{result.unverified_claim}”</p>
        </div>
      ) : null}
    </div>
  );
}

function Facts({ result }: { result: RunResult }) {
  const counts = useMemo(() => {
    const out = { VERIFIED: 0, "PARTIALLY VERIFIED": 0, UNVERIFIED: 0 };
    for (const b of result.badges) out[b.badge] += 1;
    return out;
  }, [result.badges]);
  const items: [string, string][] = [];
  if (result.mode && result.mode !== "unknown") items.push(["mode", result.mode]);
  if (result.provider) items.push(["provider", result.provider]);
  if (result.duration_s) items.push(["duration", `${result.duration_s.toFixed(0)} s`]);
  if (result.cost?.llm_calls) items.push(["model calls", String(result.cost.llm_calls)]);
  if (result.cost?.total_tokens) items.push(["tokens", result.cost.total_tokens.toLocaleString()]);
  if (result.contract_rejections.length)
    items.push(["contract rejections repaired", String(result.contract_rejections.length)]);
  if (result.report) items.push(["sandbox", result.report.sandbox]);

  return (
    <div className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
      {result.badges.length > 0 ? (
        <div className="flex items-center gap-2">
          <span className="text-muted">files</span>
          {counts.VERIFIED ? <Pill tone="ok">{counts.VERIFIED} verified</Pill> : null}
          {counts["PARTIALLY VERIFIED"] ? <Pill tone="warn">{counts["PARTIALLY VERIFIED"]} partial</Pill> : null}
          {counts.UNVERIFIED ? <Pill tone="bad">{counts.UNVERIFIED} unverified</Pill> : null}
        </div>
      ) : null}
      {items.map(([k, v]) => (
        <div key={k}>
          <span className="text-muted">{k}</span> <span className="font-medium">{v}</span>
        </div>
      ))}
      {result.report?.sandbox === "local-venv" ? (
        <span className="text-xs text-warn" title="Install Docker for enforced isolation">
          local sandbox: generated code could reach the network and your files
        </span>
      ) : null}
    </div>
  );
}

function Files({ result }: { result: RunResult }) {
  const badges = useMemo(() => new Map(result.badges.map((b) => [b.path, b])), [result.badges]);
  const sorted = [...result.files].sort((a, b) =>
    a.role === b.role ? a.path.localeCompare(b.path) : a.role === "source" ? -1 : 1,
  );
  const [open, setOpen] = useState(sorted[0]?.path ?? "");
  const file = sorted.find((f) => f.path === open);
  const badge = file ? badges.get(file.path) : undefined;

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-[260px_minmax(0,1fr)]">
      <ul className="space-y-1">
        {sorted.map((f) => {
          const b = badges.get(f.path);
          return (
            <li key={f.path}>
              <button
                onClick={() => setOpen(f.path)}
                className={`flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-sm ${
                  open === f.path ? "bg-accent-soft" : "hover:bg-surface-2"
                }`}
              >
                <span className="truncate font-mono text-xs">{f.path}</span>
                {b ? (
                  <Pill tone={toneOf(b.badge)} title={b.badge}>
                    {b.badge === "VERIFIED" ? "✓" : b.badge === "UNVERIFIED" ? "✕" : "◐"}
                  </Pill>
                ) : (
                  <span className="text-xs text-muted">{f.role === "test" ? "test" : ""}</span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
      <div className="min-w-0">
        {file ? (
          <>
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <span className="font-mono text-sm">{file.path}</span>
              {badge ? <VerdictPill verdict={badge.badge} /> : <Pill tone="muted">{file.role === "test" ? "evidence (tests)" : "not badged"}</Pill>}
              <span className="text-xs text-muted">{file.lines} lines</span>
            </div>
            {badge ? <BadgeReasons badge={badge} /> : null}
            <Code text={file.content} />
            {file.truncated ? <p className="mt-1 text-xs text-muted">Truncated for display.</p> : null}
          </>
        ) : null}
      </div>
    </div>
  );
}

function BadgeReasons({ badge }: { badge: FileBadge }) {
  if (badge.reasons.length === 0 && badge.notes.length === 0) {
    return <p className="mb-2 text-xs text-ok">Every gate ran and passed, and none reported anything serious here.</p>;
  }
  return (
    <ul className="mb-2 space-y-0.5 text-xs">
      {badge.reasons.map((r) => (
        <li key={r} className="text-warn">• {r}</li>
      ))}
      {badge.notes.map((n) => (
        <li key={n} className="text-muted">• note: {n}</li>
      ))}
    </ul>
  );
}

function Code({ text }: { text: string }) {
  const lines = text.replace(/\n$/, "").split("\n");
  return (
    <pre className="code">
      {lines.map((line, i) => (
        <div key={i} className="flex">
          <span className="w-10 shrink-0 select-none pr-3 text-right text-muted/60">{i + 1}</span>
          <code className="whitespace-pre pr-4">{line || " "}</code>
        </div>
      ))}
    </pre>
  );
}

function Gates({ results }: { results: GateResult[] }) {
  return (
    <div className="space-y-3">
      {results.map((g) => (
        <details key={g.gate_id} className="rounded-lg border border-line" open={g.status === "FAIL" || g.status === "ERROR"}>
          <summary className="flex cursor-pointer flex-wrap items-center gap-2 p-3">
            <Pill tone={toneOf(g.status)}>{g.status}</Pill>
            <span className="font-mono text-xs text-muted">{g.gate_id}</span>
            <span className="font-medium">{g.name}</span>
            {!g.blocking ? <span className="text-xs text-muted">advisory</span> : null}
            <span className="text-sm text-muted">{g.summary}</span>
          </summary>
          <div className="space-y-2 border-t border-line p-3 text-sm">
            {g.note ? <p className="text-warn">{g.note}</p> : null}
            {g.findings.length === 0 ? <p className="text-muted">No findings.</p> : null}
            <ul className="space-y-1">
              {g.findings.slice(0, 50).map((f, i) => (
                <li key={i} className="flex gap-2">
                  <Pill tone={f.severity === "high" || f.severity === "critical" ? "bad" : f.severity === "medium" ? "warn" : "muted"}>
                    {f.severity}
                  </Pill>
                  <span className="min-w-0">
                    <span className="font-mono text-xs text-muted">
                      {f.file ?? "-"}
                      {f.line ? `:${f.line}` : ""} {f.code}
                    </span>
                    <br />
                    {f.message}
                  </span>
                </li>
              ))}
            </ul>
            {g.evidence ? <p className="break-all text-xs text-muted">evidence: {g.evidence}</p> : null}
          </div>
        </details>
      ))}
    </div>
  );
}

function Specs({ result }: { result: RunResult }) {
  return (
    <div className="space-y-6">
      {result.plan ? <PlanView plan={result.plan} /> : null}
      {result.spec ? <SpecView spec={result.spec} /> : null}
      {Object.entries(result.specs).map(([name, spec]) => (
        <div key={name}>
          <h3 className="mb-2 font-mono font-semibold">{name}</h3>
          <SpecView spec={spec} />
        </div>
      ))}
    </div>
  );
}

function Repairs({ result }: { result: RunResult }) {
  return (
    <div className="space-y-3 text-sm">
      <p className="text-muted">
        Before repair: <strong>{result.initial_verdict}</strong> · after: <strong>{result.verdict}</strong>. A patch
        is kept only if more blocking gates pass and none regress; otherwise the files are restored exactly.
      </p>
      {result.repairs.map((step) => (
        <div key={step.round} className="rounded-lg border border-line p-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold">Round {step.round}</span>
            <Pill tone="accent">{step.verdict ?? "no diagnosis"}</Pill>
            <Pill tone={step.outcome === "accepted" ? "ok" : "muted"}>{step.outcome}</Pill>
          </div>
          {step.instruction ? <p className="mt-1 text-muted">{step.instruction}</p> : null}
          {step.note ? <p className="mt-1 text-xs text-muted">{step.note}</p> : null}
        </div>
      ))}
    </div>
  );
}

function Ci({ workflow }: { workflow?: string | null }) {
  const [copied, setCopied] = useState(false);
  if (!workflow) return <p className="text-sm text-muted">Loading…</p>;
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted">
        Save this as <code className="font-mono">.github/workflows/codecrew.yml</code> in the repository that holds
        this code. Every push and pull request then re-runs the same gates. No AI model and no API key are
        involved. (CLI: <code className="font-mono">codecrew ci-export</code>.)
      </p>
      <div className="flex gap-2">
        <button
          className="btn"
          onClick={async () => {
            await navigator.clipboard.writeText(workflow);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
        <a
          className="btn"
          download="codecrew.yml"
          href={`data:text/yaml;charset=utf-8,${encodeURIComponent(workflow)}`}
        >
          Download
        </a>
      </div>
      <Code text={workflow} />
    </div>
  );
}
