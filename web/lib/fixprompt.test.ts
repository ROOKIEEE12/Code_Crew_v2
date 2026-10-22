import assert from "node:assert/strict";
import { test } from "node:test";

import type { FindingView } from "./api.ts";
import { fixPrompt, sortFindings, timeAgo } from "./fixprompt.ts";

const f = (over: Partial<FindingView>): FindingView => ({
  gate: "G4",
  gate_name: "Mutation score",
  gate_status: "PASS",
  blocking: true,
  severity: "medium",
  code: "survived-mutant",
  message: "Tests still pass",
  file: "app.py",
  line: 3,
  excerpt: null,
  ...over,
});

test("blocking failures come before advisory notes, then by severity", () => {
  const sorted = sortFindings([
    f({ severity: "high", gate_status: "PASS", code: "a" }),
    f({ severity: "medium", gate_status: "FAIL", code: "b" }),
    f({ severity: "critical", gate_status: "PASS", code: "c" }),
  ]);
  assert.deepEqual(
    sorted.map((x) => x.code),
    ["b", "c", "a"],
  );
});

test("the prompt lists locations and code, and forbids weakening tests", () => {
  const text = fixPrompt(
    [f({ excerpt: { start: 2, line: 3, lines: ["b = 2", "c = a < b", "d = 4"] } }), f({ severity: "low", code: "noise" })],
    { project: "myapp", verdict: "UNVERIFIED" },
  );
  assert.match(text, /verdict is UNVERIFIED/);
  assert.match(text, /do not delete, skip or weaken any test/);
  assert.match(text, /1\. \[medium\] G4 Mutation score - app\.py:3/);
  assert.match(text, /> {4}3 \| c = a < b/);
  assert.doesNotMatch(text, /noise/, "low findings are left out");
});

test("nothing to fix is said plainly", () => {
  assert.match(fixPrompt([f({ severity: "low" })], { project: "p", verdict: "VERIFIED" }), /no medium-or-higher/);
});

test("long lists are capped and say so", () => {
  const many = Array.from({ length: 30 }, (_, i) => f({ line: i + 1 }));
  assert.match(fixPrompt(many, { project: "p", verdict: "UNVERIFIED", limit: 5 }), /25 more not listed/);
});

test("relative times", () => {
  assert.equal(timeAgo(1000, 1010), "just now");
  assert.equal(timeAgo(0, 7200), "2 h ago");
  assert.equal(timeAgo(0, 86400 * 3), "3 days ago");
});
