// node --test --experimental-strip-types lib/*.test.ts
import assert from "node:assert/strict";
import { test } from "node:test";

import type { TraceEvent } from "./api.ts";
import { describe, gateBoard, stages } from "./progress.ts";

const start = (gate: string): TraceEvent => ({ kind: "gate_progress", phase: "start", gate });
const end = (gate: string, status = "PASS"): TraceEvent => ({
  kind: "gate_progress",
  phase: "end",
  gate,
  status,
  summary: `${gate} ok`,
});

test("an empty run shows every gate pending", () => {
  const board = gateBoard([], false);
  assert.equal(board.pass, 0);
  assert.ok(board.cells.every((c) => c.state === "PENDING"));
  assert.deepEqual(
    board.cells.map((c) => c.id),
    ["G1", "G8", "G7", "G2", "G3", "G5", "G4", "G6"],
  );
});

test("a running gate is shown as running, finished ones with their status", () => {
  const board = gateBoard([start("G1"), end("G1"), start("G8")], false);
  assert.equal(board.cells[0]?.state, "PASS");
  assert.equal(board.cells[1]?.state, "RUNNING");
  assert.equal(board.cells[2]?.state, "PENDING");
});

test("gates that never reported in a finished run did not apply", () => {
  const board = gateBoard([start("G1"), end("G1")], true);
  assert.equal(board.cells[1]?.state, "N/A");
});

test("a gate reporting again starts a new verification pass", () => {
  const first = [start("G1"), end("G1"), start("G2"), end("G2", "FAIL"), end("G3", "SKIP")];
  const second = [start("G1"), end("G1"), start("G2")];
  const board = gateBoard([...first, ...second], false);
  assert.equal(board.pass, 2);
  assert.equal(board.cells.find((c) => c.id === "G2")?.state, "RUNNING");
  // G3 belongs to the first pass and must not leak into the second.
  assert.equal(board.cells.find((c) => c.id === "G3")?.state, "PENDING");
});

test("skipped gates that only report an end still count", () => {
  const board = gateBoard([end("G4", "SKIP")], true);
  assert.equal(board.cells.find((c) => c.id === "G4")?.state, "SKIP");
});

test("multi mode waits on the person between spec and code", () => {
  const events: TraceEvent[] = [
    { kind: "agent_step", agent: "spec", attempt: 1 },
    { kind: "agent_done", agent: "spec" },
  ];
  const row = stages(events, "multi", "awaiting_approval");
  assert.deepEqual(
    row.map((s) => [s.key, s.state]),
    [
      ["spec", "done"],
      ["approval", "active"],
      ["build", "todo"],
      ["gates", "todo"],
      ["verdict", "todo"],
    ],
  );
});

test("single mode has no review stage", () => {
  const row = stages([], "single", "running");
  assert.ok(!row.some((s) => s.key === "approval"));
});

test("the activity log explains rate limits instead of hiding them", () => {
  const line = describe({ kind: "llm_retry", reason: "rate_limited", sleep_s: 42 });
  assert.match(line ?? "", /Rate limited.*42s/);
  assert.equal(describe({ kind: "gate_progress", phase: "start", gate: "G1" }), null);
});
