"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Pill, VerdictPill, toneOf } from "@/components/Verdict";
import { api, type JobSummary } from "@/lib/api";
import { timeAgo } from "@/lib/fixprompt";

const KIND: Record<JobSummary["kind"], string> = { build: "AI build", verify: "Check", repair: "Repair" };

export default function RunsPage() {
  const [jobs, setJobs] = useState<JobSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () =>
      api
        .jobs()
        .then((r) => {
          setJobs(r.jobs);
          setError(null);
        })
        .catch((err) => setError(err instanceof Error ? err.message : String(err)));
    void load();
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold tracking-tight">Runs</h1>
        <p className="mt-1 text-muted">
          Everything started since <code>codecrew serve</code> was launched. Runs go one at a time; finished results stay in
          each project&apos;s history after a restart.
        </p>
      </header>
      {error ? <p className="rounded-md bg-bad-soft px-3 py-2 text-sm text-bad">{error}</p> : null}
      {jobs === null && !error ? <div className="skeleton h-40 w-full" aria-hidden /> : null}
      {jobs?.length === 0 ? (
        <div className="card p-6 text-center text-sm">
          <p className="font-semibold">Nothing has run yet.</p>
          <p className="mt-1 text-muted">
            Check a project from <Link href="/" className="text-accent underline">Projects</Link>, or{" "}
            <Link href="/build/" className="text-accent underline">build something with AI</Link>.
          </p>
        </div>
      ) : null}
      {jobs && jobs.length > 0 ? (
        <ul className="card divide-y divide-line">
          {jobs.map((job) => (
            <li key={job.id}>
              <Link href={`/job/?id=${job.id}`} className="flex flex-wrap items-center gap-3 px-4 py-3 hover:bg-surface-2">
                <Pill tone={toneOf(job.status)}>{job.status.replace("_", " ")}</Pill>
                <span className="text-xs text-muted">{KIND[job.kind]}</span>
                <span className="min-w-0 flex-1 truncate text-sm">{job.title}</span>
                <span className="text-xs text-muted">{timeAgo(job.created)}</span>
                {job.verdict ? <VerdictPill verdict={job.verdict} /> : null}
              </Link>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
