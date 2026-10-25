"use client";

import { useState } from "react";

import type { RunSummary } from "@/lib/api";
import { timeAgo } from "@/lib/fixprompt";

import { toneOf } from "./Verdict";

const W = 320;
const H = 96;
const PAD = { top: 10, right: 8, bottom: 16, left: 30 };

/**
 * One measure over the last runs, on a fixed 0-100% scale so two charts side
 * by side are comparable at a glance and a drop is never exaggerated by an
 * auto-zoomed axis. Single series: the title names it, no legend.
 */
export function TrendChart({
  runs,
  pick,
  title,
  threshold,
}: {
  runs: RunSummary[];
  pick: (r: RunSummary) => number | null;
  title: string;
  threshold?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const values = runs.map(pick);
  const known = values.filter((v): v is number => v !== null);
  const latest = known.length ? known[known.length - 1]! : null;

  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  const x = (i: number) => PAD.left + (runs.length <= 1 ? innerW / 2 : (i * innerW) / (runs.length - 1));
  const y = (v: number) => PAD.top + innerH - (Math.max(0, Math.min(100, v)) / 100) * innerH;

  // Break the line where a run has no value (the gate did not run).
  const segments: string[] = [];
  let current = "";
  values.forEach((v, i) => {
    if (v === null) {
      if (current) segments.push(current);
      current = "";
      return;
    }
    current += `${current ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
  });
  if (current) segments.push(current);

  const hovered = hover !== null ? runs[hover] : undefined;
  const step = runs.length > 1 ? innerW / (runs.length - 1) : innerW;

  return (
    <figure className="min-w-0">
      <figcaption className="mb-1 flex items-baseline justify-between gap-2">
        <span className="text-sm font-semibold">{title}</span>
        <span className="text-sm tabular-nums">
          {latest === null ? <span className="text-muted">no data</span> : `${latest.toFixed(0)}%`}
        </span>
      </figcaption>
      <div className="relative">
        <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" aria-label={`${title} over the last ${runs.length} runs`}>
          {[0, 50, 100].map((g) => (
            <g key={g}>
              <line x1={PAD.left} x2={W - PAD.right} y1={y(g)} y2={y(g)} stroke="var(--border)" strokeWidth={1} />
              <text x={PAD.left - 6} y={y(g) + 3} textAnchor="end" fontSize={9} fill="var(--muted)">
                {g}
              </text>
            </g>
          ))}
          {threshold !== undefined ? (
            <g>
              <line
                x1={PAD.left}
                x2={W - PAD.right}
                y1={y(threshold)}
                y2={y(threshold)}
                stroke="var(--muted)"
                strokeWidth={1}
                strokeDasharray="3 3"
              />
              <text x={W - PAD.right} y={y(threshold) - 3} textAnchor="end" fontSize={9} fill="var(--muted)">
                threshold {threshold}%
              </text>
            </g>
          ) : null}
          {segments.map((d, i) => (
            <path key={i} d={d} fill="none" stroke="var(--accent)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
          ))}
          {values.map((v, i) =>
            v === null ? null : (
              <circle
                key={i}
                cx={x(i)}
                cy={y(v)}
                r={hover === i ? 4.5 : i === values.length - 1 ? 3 : 0}
                fill="var(--accent)"
                stroke="var(--surface)"
                strokeWidth={2}
              />
            ),
          )}
          {hover !== null ? (
            <line x1={x(hover)} x2={x(hover)} y1={PAD.top} y2={H - PAD.bottom} stroke="var(--muted)" strokeWidth={1} />
          ) : null}
          {runs.map((_, i) => (
            <rect
              key={i}
              x={x(i) - step / 2}
              y={0}
              width={Math.max(step, 12)}
              height={H}
              fill="transparent"
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
            />
          ))}
          <text x={PAD.left} y={H - 3} fontSize={9} fill="var(--muted)">
            older
          </text>
          <text x={W - PAD.right} y={H - 3} textAnchor="end" fontSize={9} fill="var(--muted)">
            latest
          </text>
        </svg>
        {hovered ? (
          <div
            className="pointer-events-none absolute -top-2 z-10 -translate-x-1/2 -translate-y-full whitespace-nowrap rounded-md border border-line bg-surface px-2 py-1 text-xs shadow"
            style={{ left: `${(x(hover!) / W) * 100}%` }}
          >
            <div className="font-semibold tabular-nums">
              {values[hover!] === null ? "did not run" : `${values[hover!]!.toFixed(1)}%`}
            </div>
            <div className="text-muted">
              {timeAgo(hovered.time)} · {hovered.profile}
            </div>
          </div>
        ) : null}
      </div>
      <table className="sr-only">
        <caption>{title} by run</caption>
        <tbody>
          {runs.map((r, i) => (
            <tr key={r.run_id}>
              <td>{new Date(r.time * 1000).toLocaleString()}</td>
              <td>{values[i] === null ? "did not run" : `${values[i]!.toFixed(1)}%`}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}

const DOT: Record<string, string> = { ok: "bg-ok", warn: "bg-warn", bad: "bg-bad", muted: "bg-line", accent: "bg-accent" };
const MARK: Record<string, string> = { VERIFIED: "✓", "PARTIALLY VERIFIED": "◐", UNVERIFIED: "✕" };

/** The last runs' verdicts, oldest to newest. Each dot carries its label. */
export function VerdictDots({ runs, size = "sm" }: { runs: RunSummary[]; size?: "sm" | "md" }) {
  if (runs.length === 0) return null;
  const px = size === "md" ? "h-5 w-5 text-[10px]" : "h-4 w-4 text-[9px]";
  return (
    <ol className="flex items-center gap-1" aria-label="Recent verdicts, oldest first">
      {runs.map((r) => (
        <li
          key={r.run_id}
          title={`${r.verdict ?? "no verdict"} · ${timeAgo(r.time)} · ${r.profile}`}
          className={`grid ${px} place-items-center rounded-full font-bold text-white ${DOT[toneOf(r.verdict)]}`}
        >
          <span aria-hidden>{MARK[r.verdict ?? ""] ?? "•"}</span>
          <span className="sr-only">{r.verdict}</span>
        </li>
      ))}
    </ol>
  );
}
