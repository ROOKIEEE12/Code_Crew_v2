import type { ReactNode } from "react";

type Tone = "ok" | "warn" | "bad" | "muted" | "accent";

const TONE: Record<Tone, string> = {
  ok: "bg-ok-soft text-ok border-ok/30",
  warn: "bg-warn-soft text-warn border-warn/30",
  bad: "bg-bad-soft text-bad border-bad/30",
  muted: "bg-surface-2 text-muted border-line",
  accent: "bg-accent-soft text-accent border-accent/30",
};

const TEXT: Record<Tone, string> = {
  ok: "text-ok",
  warn: "text-warn",
  bad: "text-bad",
  muted: "text-fg",
  accent: "text-accent",
};

export function toneOf(value: string | null | undefined): Tone {
  switch (value) {
    case "VERIFIED":
    case "PASS":
    case "done":
      return "ok";
    case "PARTIALLY VERIFIED":
    case "SKIP":
    case "awaiting_approval":
      return "warn";
    case "UNVERIFIED":
    case "FAILED TO BUILD":
    case "FAIL":
    case "ERROR":
    case "failed":
      return "bad";
    case "running":
    case "RUNNING":
      return "accent";
    default:
      return "muted";
  }
}

export function Pill({ tone, children, title }: { tone: Tone; children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-semibold ${TONE[tone]}`}
    >
      {children}
    </span>
  );
}

const ICON: Record<string, string> = {
  VERIFIED: "✓",
  "PARTIALLY VERIFIED": "◐",
  UNVERIFIED: "✕",
  "FAILED TO BUILD": "✕",
};

export function VerdictPill({ verdict }: { verdict: string | null | undefined }) {
  if (!verdict) return <Pill tone="muted">no verdict yet</Pill>;
  return (
    <Pill tone={toneOf(verdict)}>
      <span aria-hidden>{ICON[verdict] ?? "•"}</span>
      {verdict}
    </Pill>
  );
}

const EXPLAIN: Record<string, string> = {
  VERIFIED: "Every gate ran and passed. This is the gates' result, not an agent's opinion.",
  "PARTIALLY VERIFIED":
    "Nothing blocking failed, but at least one check did not run, so part of the code is unchecked.",
  UNVERIFIED: "A blocking gate failed. Do not ship this without reading the findings.",
  "FAILED TO BUILD": "No code was produced, so there is nothing to verify.",
  "NOT VERIFIED": "The gates have not run on this workspace.",
};

export function VerdictBanner({
  verdict,
  note,
  initial,
}: {
  verdict: string;
  note?: string | null;
  initial?: string | null;
}) {
  const tone = toneOf(verdict);
  const border = { ok: "border-ok", warn: "border-warn", bad: "border-bad", muted: "border-line", accent: "border-accent" }[tone];
  return (
    <div className={`card border-l-4 ${border} p-5`}>
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-xs font-semibold uppercase tracking-wider text-muted">Verdict</span>
        <span className={`text-2xl font-bold ${TEXT[tone]}`}>{verdict}</span>
        {initial && initial !== verdict ? (
          <Pill tone="muted" title="The verdict before the repair loop ran">
            before repair: {initial}
          </Pill>
        ) : null}
      </div>
      <p className="mt-1 text-sm text-muted">{EXPLAIN[verdict] ?? ""}</p>
      {note ? (
        <p className="mt-3 rounded-md bg-warn-soft px-3 py-2 text-sm text-warn">
          <strong>{verdict} {note}.</strong> The gates check the code against the spec; nobody checked
          the spec against what you meant. Read its assumptions.
        </p>
      ) : null}
    </div>
  );
}
