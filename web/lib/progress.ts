// Derives what the job page shows from the raw trace events.
//
// Pure functions over the event list, so the same events always produce the
// same board, and they can be tested without a browser.

import type { GateStatus, TraceEvent } from "./api.ts";

/** Execution order, cheapest first (mirrors codecrew/gates/runner.py). */
export const GATES: { id: string; name: string; what: string }[] = [
  { id: "G1", name: "Syntax, lint & types", what: "Does it compile and type-check?" },
  { id: "G8", name: "Dependencies", what: "Are imports real, declared packages?" },
  { id: "G7", name: "Spec conformance", what: "Do the spec's examples hold?" },
  { id: "G2", name: "Tests", what: "Do the generated tests pass?" },
  { id: "G3", name: "Branch coverage", what: "Did the tests reach every branch?" },
  { id: "G5", name: "Security", what: "Bandit + secret scan" },
  { id: "G4", name: "Mutation score", what: "Would the tests notice a bug?" },
  { id: "G6", name: "Property tests", what: "Edge cases beyond hand-picked inputs" },
];

export type BoardState = GateStatus | "PENDING" | "RUNNING" | "N/A";

export interface GateCell {
  id: string;
  state: BoardState;
  summary?: string;
  score?: number | null;
  note?: string | null;
  blocking?: boolean;
}

export interface Board {
  /** 1 for the first verification, +1 for each re-verification after a patch. */
  pass: number;
  cells: GateCell[];
}

/**
 * The gate board for the latest verification pass.
 *
 * Every pass reports each applicable gate once, so a gate reporting again
 * after it already finished means a new pass (a re-verification after a
 * repair patch). Gates that never report in a finished run did not apply.
 */
export function gateBoard(events: TraceEvent[], finished: boolean): Board {
  let pass = 0;
  let cells = new Map<string, GateCell>();

  for (const event of events) {
    if (event.kind !== "gate_progress") continue;
    const gate = String(event.gate ?? "");
    const previous = cells.get(gate);
    if (pass === 0 || (previous && previous.state !== "RUNNING")) {
      pass += 1;
      cells = new Map();
    }
    if (event.phase === "start") {
      cells.set(gate, { id: gate, state: "RUNNING" });
    } else if (event.phase === "end") {
      cells.set(gate, {
        id: gate,
        state: (event.status as GateStatus) ?? "ERROR",
        summary: event.summary as string | undefined,
        score: (event.score as number | null | undefined) ?? null,
        note: (event.note as string | null | undefined) ?? null,
        blocking: event.blocking as boolean | undefined,
      });
    }
  }

  return {
    pass,
    cells: GATES.map(
      ({ id }) =>
        cells.get(id) ?? {
          id,
          state: finished && pass > 0 ? "N/A" : "PENDING",
        },
    ),
  };
}

export type StageState = "todo" | "active" | "done" | "skipped" | "failed";

export interface Stage {
  key: string;
  label: string;
  state: StageState;
  detail?: string;
}

const AGENT_LABEL: Record<string, string> = {
  architect: "Architect",
  spec: "Spec writer",
  implementer: "Implementer",
  test_author: "Test author",
  coder: "Coder (single agent)",
  diagnostician: "Diagnostician",
  patcher: "Patcher",
  spec_repair: "Spec repair",
};

export function agentLabel(name: unknown): string {
  return AGENT_LABEL[String(name)] ?? String(name);
}

/** The pipeline as a row of stages, for the stepper at the top of a job. */
export function stages(
  events: TraceEvent[],
  mode: string,
  status: string,
): Stage[] {
  const kinds = new Set(events.map((e) => e.kind));
  const agentsDone = new Set(
    events.filter((e) => e.kind === "agent_done").map((e) => String(e.agent)),
  );
  const agentsStarted = new Set(
    events.filter((e) => e.kind === "agent_step").map((e) => String(e.agent)),
  );
  const ended = status === "done" || status === "failed" || status === "cancelled";
  const failed = status === "failed" || kinds.has("agent_failed");

  const state = (done: boolean, active: boolean): StageState =>
    done ? "done" : active ? (ended ? (failed ? "failed" : "skipped") : "active") : ended ? "skipped" : "todo";

  const out: Stage[] = [];
  if (mode === "project") {
    out.push({
      key: "plan",
      label: "Plan",
      state: state(kinds.has("plan_decision") || kinds.has("plan_supplied"), agentsStarted.has("architect")),
    });
  }
  if (mode !== "single") {
    const decided = events.some((e) => e.kind === "spec_decision" || e.kind === "spec_supplied");
    const waiting = status === "awaiting_approval";
    out.push({
      key: "spec",
      label: "Spec",
      state: state(agentsDone.has("spec") || kinds.has("spec_supplied"), agentsStarted.has("spec")),
    });
    out.push({
      key: "approval",
      label: "Your review",
      state: waiting ? "active" : state(decided || kinds.has("files_written"), false),
    });
    out.push({
      key: "build",
      label: "Code + tests",
      detail: "written independently",
      state: state(kinds.has("files_written"), agentsStarted.has("implementer") || agentsStarted.has("test_author")),
    });
  } else {
    out.push({
      key: "build",
      label: "Code + tests",
      detail: "one agent",
      state: state(kinds.has("files_written"), agentsStarted.has("coder")),
    });
  }

  const gatesStarted = events.some((e) => e.kind === "gate_progress");
  out.push({
    key: "gates",
    label: "Gates",
    state: state(kinds.has("badges") || kinds.has("run_end"), gatesStarted),
  });
  if (kinds.has("repair_round") || kinds.has("repair_start")) {
    out.push({
      key: "repair",
      label: "Repair",
      state: state(kinds.has("run_end"), true),
    });
  }
  out.push({
    key: "verdict",
    label: "Verdict",
    state: kinds.has("run_end") || (ended && !failed) ? "done" : failed ? "failed" : "todo",
  });
  return out;
}

/** Human line for one event in the activity log; null hides it. */
export function describe(event: TraceEvent): string | null {
  const agent = agentLabel(event.agent);
  switch (event.kind) {
    case "queued":
      return "Queued";
    case "job_start":
      return "Started";
    case "run_start":
      return `Run started (${event.mode} mode, provider ${event.provider})`;
    case "agent_context": {
      const keys = (event.keys as string[] | undefined)?.join(", ") ?? "";
      return `${agent} was given only: ${keys}`;
    }
    case "agent_step":
      return `${agent} is working (attempt ${event.attempt})`;
    case "contract_rejected":
      return `${agent}'s output was rejected by its contract: ${event.reason}`;
    case "agent_done":
      return `${agent} finished`;
    case "agent_failed":
      return `${agent} gave up after ${event.attempts} attempts`;
    case "llm_call": {
      const tokens =
        typeof event.prompt_tokens === "number" || typeof event.completion_tokens === "number"
          ? `${Number(event.prompt_tokens ?? 0) + Number(event.completion_tokens ?? 0)} tokens`
          : "tokens not reported";
      return `Model call: ${agent}, ${event.model ?? event.provider} (${tokens}, ${event.latency_s ?? "?"}s)`;
    }
    case "llm_retry":
      return event.reason === "rate_limited"
        ? `Rate limited by the free tier; waiting ${event.sleep_s}s`
        : `Model call failed (${event.reason}); retrying`;
    case "approval_requested":
      return `Waiting for your review of the ${event.type}`;
    case "approval_decided":
      return event.approved ? `You approved the ${event.type}` : `You rejected the ${event.type}`;
    case "approval_timeout":
      return `Nobody answered; the ${event.type} counts as rejected`;
    case "files_written":
      return `Wrote ${event.count} file(s)`;
    case "gate_progress":
      return event.phase === "end"
        ? `${event.gate} ${event.name}: ${event.status} - ${event.summary}`
        : null;
    case "repair_round":
      return `Repair round ${event.round}: blocked by ${(event.blocked as string[]).join(", ")}`;
    case "repair_diagnosis":
      return `Diagnosis: ${event.verdict} - ${event.instruction ?? ""}`;
    case "repair_outcome":
      return `Repair round ${event.round}: ${event.outcome}${event.note ? ` (${event.note})` : ""}`;
    case "run_end":
      return `Run finished: ${event.outcome}`;
    case "job_end":
      return `Job ${event.status}`;
    default:
      return null;
  }
}
