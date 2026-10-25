// Turns findings into text a developer can paste into their AI assistant.
//
// Deterministic: built from the gate report only, so it states exactly what
// the gates found, ranked by what blocks, and asks for the one thing that
// matters (fix it without weakening the tests). No model writes this.

import type { FindingView } from "./api.ts";

const RANK: Record<FindingView["severity"], number> = {
  critical: 4,
  high: 3,
  medium: 2,
  low: 1,
  info: 0,
};

export function sortFindings(findings: FindingView[]): FindingView[] {
  return [...findings].sort(
    (a, b) =>
      Number(b.blocking && b.gate_status !== "PASS") - Number(a.blocking && a.gate_status !== "PASS") ||
      RANK[b.severity] - RANK[a.severity] ||
      (a.file ?? "").localeCompare(b.file ?? "") ||
      (a.line ?? 0) - (b.line ?? 0),
  );
}

export function fixPrompt(
  findings: FindingView[],
  { project, verdict, limit = 25 }: { project: string; verdict: string; limit?: number },
): string {
  const relevant = sortFindings(findings).filter((f) => RANK[f.severity] >= RANK.medium);
  if (relevant.length === 0) {
    return `CodeCrew found no medium-or-higher problems in ${project} (verdict: ${verdict}).`;
  }
  const shown = relevant.slice(0, limit);
  const lines = [
    `CodeCrew verified ${project} and the verdict is ${verdict}. Fix these problems.`,
    "Rules: do not delete, skip or weaken any test, and do not lower thresholds;",
    "change the code (or add a test) so the check passes for the right reason.",
    "",
  ];
  shown.forEach((f, i) => {
    const where = f.file ? `${f.file}${f.line ? `:${f.line}` : ""}` : "(no location)";
    lines.push(`${i + 1}. [${f.severity}] ${f.gate} ${f.gate_name} - ${where}`);
    lines.push(`   ${f.code}: ${oneLine(f.message)}`);
    if (f.excerpt) {
      f.excerpt.lines.forEach((text, k) => {
        const n = f.excerpt!.start + k;
        lines.push(`   ${n === f.excerpt!.line ? ">" : " "} ${String(n).padStart(4)} | ${text}`);
      });
    }
  });
  if (relevant.length > shown.length) {
    lines.push("", `(${relevant.length - shown.length} more not listed; fix these first.)`);
  }
  lines.push("", "Afterwards I will re-run `codecrew verify` to check.");
  return lines.join("\n");
}

function oneLine(text: string): string {
  return text.replace(/\s+/g, " ").trim().slice(0, 400);
}

export function timeAgo(epochSeconds: number, now = Date.now() / 1000): string {
  const s = Math.max(0, Math.round(now - epochSeconds));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const d = Math.round(s / 86400);
  return d === 1 ? "yesterday" : `${d} days ago`;
}
