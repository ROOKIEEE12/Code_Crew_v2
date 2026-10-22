"use client";

import { useMemo, useState } from "react";

import type { FindingView } from "@/lib/api";
import { fixPrompt, sortFindings } from "@/lib/fixprompt";

import { useToast } from "./Toast";
import { Pill } from "./Verdict";

type Level = "problems" | "all";

const SEVERITY_TONE = {
  critical: "bad",
  high: "bad",
  medium: "warn",
  low: "muted",
  info: "muted",
} as const;

export function Findings({
  findings,
  project,
  verdict,
}: {
  findings: FindingView[];
  project: string;
  verdict: string;
}) {
  const toast = useToast();
  const [level, setLevel] = useState<Level>("problems");
  const [gate, setGate] = useState<string>("all");

  const gates = useMemo(() => [...new Set(findings.map((f) => f.gate))].sort(), [findings]);
  const visible = useMemo(
    () =>
      sortFindings(findings).filter(
        (f) =>
          (level === "all" || ["critical", "high", "medium"].includes(f.severity)) &&
          (gate === "all" || f.gate === gate),
      ),
    [findings, level, gate],
  );
  const groups = useMemo(() => {
    const map = new Map<string, FindingView[]>();
    for (const f of visible) {
      const key = f.file ?? "(project-wide)";
      map.set(key, [...(map.get(key) ?? []), f]);
    }
    return [...map.entries()];
  }, [visible]);

  const problems = findings.filter((f) => ["critical", "high", "medium"].includes(f.severity)).length;

  async function copy() {
    const text = fixPrompt(findings, { project, verdict });
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied. Paste it into your AI assistant, then run the check again.", "ok");
    } catch {
      toast("Could not copy; your browser blocked the clipboard.", "bad");
    }
  }

  if (findings.length === 0) {
    return (
      <div className="rounded-lg border border-line bg-surface-2/50 p-6 text-center text-sm">
        <p className="font-semibold">No findings.</p>
        <p className="mt-1 text-muted">Every gate that ran reported nothing about specific lines of code.</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <div role="radiogroup" aria-label="Which findings" className="inline-flex rounded-lg border border-line p-0.5">
          {(
            [
              ["problems", `Problems (${problems})`],
              ["all", `Everything (${findings.length})`],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              role="radio"
              aria-checked={level === value}
              onClick={() => setLevel(value)}
              className={`rounded-md px-3 py-1 text-sm ${level === value ? "bg-accent-soft font-semibold text-accent" : "text-muted"}`}
            >
              {label}
            </button>
          ))}
        </div>
        <label className="flex items-center gap-2 text-sm text-muted">
          Gate
          <select className="field w-auto py-1" value={gate} onChange={(e) => setGate(e.target.value)}>
            <option value="all">all</option>
            {gates.map((g) => (
              <option key={g}>{g}</option>
            ))}
          </select>
        </label>
        <button className="btn ml-auto" onClick={copy} disabled={problems === 0} title="Every medium-or-higher finding, with code, as a prompt">
          ⧉ Copy fix prompt
        </button>
      </div>

      {groups.length === 0 ? (
        <p className="text-sm text-muted">
          Nothing at this level. {level === "problems" ? "Switch to Everything to see informational notes." : ""}
        </p>
      ) : null}

      {groups.map(([file, items]) => (
        <section key={file} className="rounded-lg border border-line">
          <header className="flex items-center gap-2 border-b border-line bg-surface-2/60 px-3 py-2">
            <span className="min-w-0 flex-1 truncate font-mono text-xs" title={file}>
              {file}
            </span>
            <span className="text-xs text-muted">{items.length}</span>
          </header>
          <ul className="divide-y divide-line">
            {byLine(items).map(([line, group]) => (
              <li key={line} className="p-3">
                <ul className="space-y-2">
                  {group.map((f, i) => (
                    <li key={i}>
                      <div className="flex flex-wrap items-center gap-2 text-sm">
                        <Pill tone={SEVERITY_TONE[f.severity]}>{f.severity}</Pill>
                        <span className="font-mono text-xs text-muted">
                          {f.gate}
                          {f.line ? ` · line ${f.line}` : ""} · {f.code}
                        </span>
                        {f.gate_status !== "PASS" && f.blocking ? <Pill tone="bad">blocks the verdict</Pill> : null}
                      </div>
                      <p className="mt-1 text-sm">{f.message}</p>
                      {explain(f) ? <p className="mt-0.5 text-xs text-muted">{explain(f)}</p> : null}
                    </li>
                  ))}
                </ul>
                {group[0]?.excerpt ? <Excerpt excerpt={group[0].excerpt} /> : null}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

/** Findings on the same line share one code excerpt. */
function byLine(items: FindingView[]): [string, FindingView[]][] {
  const map = new Map<string, FindingView[]>();
  items.forEach((f, i) => {
    const key = f.line ? `L${f.line}` : `n${i}`;
    map.set(key, [...(map.get(key) ?? []), f]);
  });
  return [...map.entries()];
}

/** What to do about a finding, in plain words, for the common kinds. */
export function explain(f: FindingView): string | null {
  if (f.code.startsWith("survived-mutant")) {
    return "CodeCrew changed this line on purpose and every test still passed, so a real bug here would go unnoticed. Add a test whose result depends on this exact condition or value.";
  }
  if (f.code === "low-coverage") return "Some branches here never run in the tests. Add tests for the paths that are missed (often the error cases).";
  if (f.code === "test-failure") return "A test fails. Either the code is wrong or the test expects the wrong thing; the message says which assertion.";
  if (f.code === "undeclared-dependency") return "Declare the package in pyproject.toml or requirements.txt, so the project installs the same way everywhere.";
  if (f.code.startsWith("bandit:") || f.code.startsWith("secret")) return "A security scanner flagged this pattern. Check whether untrusted input can reach it.";
  if (f.code === "mypy:error") return "The type checker found values used inconsistently with their declared types.";
  return null;
}

function Excerpt({ excerpt }: { excerpt: NonNullable<FindingView["excerpt"]> }) {
  return (
    <pre className="code mt-2 py-1">
      {excerpt.lines.map((text, k) => {
        const n = excerpt.start + k;
        const hit = n === excerpt.line;
        return (
          <div key={n} className={`flex ${hit ? "bg-warn-soft" : ""}`}>
            <span className="w-12 shrink-0 select-none pr-3 text-right text-muted/70">{n}</span>
            <code className={`whitespace-pre pr-4 ${hit ? "font-semibold" : ""}`}>{text || " "}</code>
          </div>
        );
      })}
    </pre>
  );
}
