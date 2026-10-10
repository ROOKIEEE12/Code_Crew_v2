"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { GateBoard } from "@/components/GateBoard";
import { ResultView } from "@/components/ResultView";
import { ApprovalPanel } from "@/components/SpecReview";
import { Stepper } from "@/components/Stepper";
import { Pill, toneOf } from "@/components/Verdict";
import { api, type JobDetail, type TraceEvent } from "@/lib/api";
import { describe, gateBoard, stages } from "@/lib/progress";

const ENDED = new Set(["done", "failed", "cancelled"]);

export function JobView() {
  const id = useSearchParams().get("id") ?? "";
  const [job, setJob] = useState<JobDetail | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [workflow, setWorkflow] = useState<string | null>(null);
  const next = useRef(0);

  const poll = useCallback(async () => {
    if (!id) return;
    try {
      const detail = await api.job(id, next.current);
      next.current = detail.next;
      if (detail.events.length) setEvents((prev) => [...prev, ...detail.events]);
      setJob(detail);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [id]);

  useEffect(() => {
    next.current = 0;
    setEvents([]);
    void poll();
  }, [poll]);

  const ended = job ? ENDED.has(job.status) : false;
  useEffect(() => {
    if (ended) return;
    const timer = setInterval(poll, 1000);
    return () => clearInterval(timer);
  }, [poll, ended]);

  if (!id) return <p className="text-muted">No job id in the address.</p>;
  if (!job) return <p className="text-muted">{error ?? "Loading…"}</p>;

  const mode = String(job.params.mode ?? (job.kind === "verify" ? "verify" : "multi"));
  const board = gateBoard(events, ended);
  const log = events.map((e) => ({ e, text: describe(e) })).filter((x) => x.text);

  return (
    <div className="space-y-6">
      <div>
        <Link href="/" className="text-sm text-muted hover:text-fg">
          ← All builds
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="min-w-0 flex-1 break-words text-xl font-bold">{job.title}</h1>
          <Pill tone={toneOf(job.status)}>{job.status.replace("_", " ")}</Pill>
          {job.status === "queued" || job.status === "awaiting_approval" ? (
            <button className="btn text-bad" onClick={() => api.cancel(job.id).then(poll)}>
              Cancel
            </button>
          ) : null}
        </div>
        {job.workspace ? <p className="mt-1 break-all font-mono text-xs text-muted">{job.workspace}</p> : null}
      </div>

      {job.kind !== "verify" ? <Stepper stages={stages(events, mode, job.status)} /> : null}

      {error ? <p className="rounded-md bg-bad-soft px-3 py-2 text-sm text-bad">{error}</p> : null}
      {job.status === "queued" ? (
        <p className="card p-4 text-sm text-muted">
          Waiting for the job ahead of it. Builds run one at a time because they share the free model quota and
          the sandbox.
        </p>
      ) : null}
      {job.error ? (
        <div className="card border-bad/40 bg-bad-soft p-4 text-sm text-bad">
          <strong>The job failed:</strong> {job.error}
        </div>
      ) : null}

      {job.pending ? <ApprovalPanel key={job.pending.requested} jobId={job.id} pending={job.pending} onDecided={poll} /> : null}

      {job.result ? (
        <ResultView
          result={job.result}
          workflow={workflow}
          onLoadWorkflow={() => api.workflow(job.id).then((w) => setWorkflow(w.workflow))}
        />
      ) : null}

      {events.some((e) => e.kind === "gate_progress") || (!job.result && job.status === "running") ? (
        <GateBoard board={board} />
      ) : null}

      <section className="card p-4">
        <h2 className="mb-2 font-semibold">Activity</h2>
        <p className="mb-3 text-xs text-muted">
          Every agent step, model call and gate result, from the run&apos;s trace. Agents are listed with exactly the
          inputs they were allowed to see.
        </p>
        <ol className="max-h-96 space-y-1 overflow-y-auto font-mono text-xs">
          {log.map(({ e, text }, i) => (
            <li key={i} className="flex gap-3">
              <span className="w-14 shrink-0 text-right text-muted">{typeof e.t === "number" ? `${e.t.toFixed(1)}s` : ""}</span>
              <span className={e.kind === "contract_rejected" || e.kind === "agent_failed" ? "text-warn" : ""}>{text}</span>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}
