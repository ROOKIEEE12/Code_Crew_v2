"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { Pill, VerdictPill, toneOf } from "@/components/Verdict";
import {
  api,
  type BuildRequest,
  type Health,
  type JobSummary,
  type WorkspaceSummary,
} from "@/lib/api";

const EXAMPLES = [
  "A function that validates a password: at least 12 characters, one digit, one uppercase letter, and not one of the 20 most common passwords. Return a list of the rules it breaks.",
  "Parse a duration like '1h30m', '45s' or '2h' into total seconds. Reject anything else with ValueError.",
  "Merge overlapping intervals given as (start, end) tuples and return them sorted.",
];

const MODES: { value: BuildRequest["mode"]; label: string; help: string }[] = [
  {
    value: "multi",
    label: "Multi-agent (recommended)",
    help: "Spec, your review, then an implementer and a test author who never see each other's work.",
  },
  {
    value: "single",
    label: "Single agent (baseline)",
    help: "One agent writes code and its own tests, like a typical AI assistant. Same gates judge it.",
  },
  {
    value: "project",
    label: "Project (several modules)",
    help: "An architect splits the requirement into modules; each is built the multi-agent way.",
  },
];

export default function Home() {
  const router = useRouter();
  const [health, setHealth] = useState<Health | null>(null);
  const [offline, setOffline] = useState<string | null>(null);
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([]);

  const [requirement, setRequirement] = useState("");
  const [mode, setMode] = useState<BuildRequest["mode"]>("multi");
  const [provider, setProvider] = useState("auto");
  const [script, setScript] = useState("");
  const [repair, setRepair] = useState(2);
  const [approval, setApproval] = useState<"review" | "auto">("review");
  const [verifyPath, setVerifyPath] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [j, w] = await Promise.all([api.jobs(), api.workspaces()]);
      setJobs(j.jobs);
      setWorkspaces(w.workspaces);
      setOffline(null);
    } catch (err) {
      setOffline(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    api
      .health()
      .then((h) => {
        setHealth(h);
        const live = h.providers.find((p) => p.ready && p.name !== "scripted");
        if (!live) setProvider("scripted");
        if (h.replays[0]) setScript(h.replays.includes("clamp_multi.json") ? "clamp_multi.json" : h.replays[0]);
      })
      .catch((err) => setOffline(err instanceof Error ? err.message : String(err)));
    void refresh();
    const timer = setInterval(refresh, 4000);
    return () => clearInterval(timer);
  }, [refresh]);

  async function submit(body: Parameters<typeof api.submit>[0]) {
    setBusy(true);
    setError(null);
    try {
      const job = await api.submit(body);
      router.push(`/job/?id=${job.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  const ready = health?.providers.filter((p) => p.ready) ?? [];
  const live = ready.filter((p) => p.name !== "scripted");

  return (
    <div className="space-y-8">
      <section className="max-w-3xl">
        <h1 className="text-3xl font-bold tracking-tight">Code you can check, not just code that looks right.</h1>
        <p className="mt-2 text-muted">
          Describe what you need. AI agents write the code and the tests; then eight deterministic gates (tests,
          branch coverage, mutation testing, security scans, spec conformance) decide the verdict. No agent gets to
          say its own work is done.
        </p>
      </section>

      {offline ? (
        <div className="card border-bad/40 bg-bad-soft p-4 text-sm text-bad">
          {offline}
          <pre className="code mt-2 px-3 text-fg">codecrew serve</pre>
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
        <form
          className="card space-y-5 p-5"
          onSubmit={(e) => {
            e.preventDefault();
            void submit({
              kind: "build",
              requirement,
              mode,
              provider,
              repair: mode === "single" ? 0 : repair,
              approval,
              ...(provider === "scripted" ? { script } : {}),
            });
          }}
        >
          <div>
            <label htmlFor="req" className="mb-1 block font-semibold">
              What should it do?
            </label>
            <textarea
              id="req"
              className="field h-36"
              placeholder="Plain language is fine. Say what should happen on bad input; anything you leave out becomes an assumption you will be asked to check."
              value={requirement}
              onChange={(e) => setRequirement(e.target.value)}
              maxLength={8000}
              required
            />
            <div className="mt-1 flex flex-wrap gap-2 text-xs">
              <span className="text-muted">Try:</span>
              {EXAMPLES.map((ex) => (
                <button type="button" key={ex} className="text-accent hover:underline" onClick={() => setRequirement(ex)}>
                  {ex.split(":")[0]!.split(" ").slice(0, 5).join(" ")}…
                </button>
              ))}
            </div>
          </div>

          <fieldset>
            <legend className="mb-2 font-semibold">How should it be built?</legend>
            <div className="grid gap-2 sm:grid-cols-3">
              {MODES.map((m) => (
                <label
                  key={m.value}
                  className={`cursor-pointer rounded-lg border p-3 text-sm ${
                    mode === m.value ? "border-accent bg-accent-soft" : "border-line"
                  }`}
                >
                  <input
                    type="radio"
                    name="mode"
                    className="sr-only"
                    checked={mode === m.value}
                    onChange={() => setMode(m.value)}
                  />
                  <span className="font-semibold">{m.label}</span>
                  <span className="mt-1 block text-xs text-muted">{m.help}</span>
                </label>
              ))}
            </div>
          </fieldset>

          <div className="grid gap-4 sm:grid-cols-3">
            <div>
              <label htmlFor="provider" className="mb-1 block text-sm font-semibold">
                Model
              </label>
              <select id="provider" className="field" value={provider} onChange={(e) => setProvider(e.target.value)}>
                {live.length > 0 ? <option value="auto">auto (first available free tier)</option> : null}
                {live.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.name}
                  </option>
                ))}
                <option value="scripted">scripted replay (offline demo)</option>
              </select>
            </div>
            {provider === "scripted" ? (
              <div>
                <label htmlFor="script" className="mb-1 block text-sm font-semibold">
                  Replay
                </label>
                <select id="script" className="field" value={script} onChange={(e) => setScript(e.target.value)}>
                  {(health?.replays ?? []).map((r) => (
                    <option key={r}>{r}</option>
                  ))}
                </select>
              </div>
            ) : null}
            {mode !== "single" ? (
              <>
                <div>
                  <label htmlFor="repair" className="mb-1 block text-sm font-semibold">
                    Repair rounds
                  </label>
                  <select id="repair" className="field" value={repair} onChange={(e) => setRepair(Number(e.target.value))}>
                    {[0, 1, 2, 3, 4].map((n) => (
                      <option key={n} value={n}>
                        {n === 0 ? "0 (off)" : n}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label htmlFor="approval" className="mb-1 block text-sm font-semibold">
                    Spec review
                  </label>
                  <select
                    id="approval"
                    className="field"
                    value={approval}
                    onChange={(e) => setApproval(e.target.value as "review" | "auto")}
                  >
                    <option value="review">I review it first</option>
                    <option value="auto">Auto-approve (verdict will say so)</option>
                  </select>
                </div>
              </>
            ) : null}
          </div>

          {provider === "scripted" ? (
            <p className="text-xs text-muted">
              A scripted replay feeds recorded model answers, so the demo runs offline and free. Your requirement text is
              recorded but does not change the replayed code; the gates still run for real.
            </p>
          ) : null}
          {error ? <p className="rounded-md bg-bad-soft px-3 py-2 text-sm text-bad">{error}</p> : null}

          <button className="btn btn-primary" disabled={busy || !requirement.trim() || !!offline}>
            {busy ? "Starting…" : "Build and verify"}
          </button>
        </form>

        <aside className="space-y-4">
          <div className="card p-4 text-sm">
            <h2 className="mb-2 font-semibold">Status</h2>
            {health ? (
              <ul className="space-y-1">
                {health.providers.map((p) => (
                  <li key={p.name} className="flex items-center justify-between gap-2">
                    <span>{p.name}</span>
                    <Pill tone={p.ready ? "ok" : "muted"} title={p.reason}>
                      {p.ready ? "ready" : "not set up"}
                    </Pill>
                  </li>
                ))}
                <li className="flex items-center justify-between gap-2 border-t border-line pt-1">
                  <span>sandbox</span>
                  <Pill tone={health.sandbox.docker ? "ok" : "warn"}>{health.sandbox.docker ? "docker" : "local venv"}</Pill>
                </li>
              </ul>
            ) : (
              <p className="text-muted">Connecting…</p>
            )}
            {health && live.length === 0 ? (
              <p className="mt-2 text-xs text-muted">
                No live model configured. Set GROQ_API_KEY or GEMINI_API_KEY (both free), or run Ollama, then restart{" "}
                <code>codecrew serve</code>.
              </p>
            ) : null}
          </div>

          <form
            className="card space-y-2 p-4 text-sm"
            onSubmit={(e) => {
              e.preventDefault();
              void submit({ kind: "verify", workspace: verifyPath });
            }}
          >
            <h2 className="font-semibold">Verify existing code</h2>
            <p className="text-xs text-muted">Run only the gates, no AI, on a folder inside {health?.roots[0] ?? "the workspace root"}.</p>
            <input
              className="field font-mono text-xs"
              placeholder="my-project"
              value={verifyPath}
              onChange={(e) => setVerifyPath(e.target.value)}
              required
            />
            <button className="btn" disabled={busy || !verifyPath.trim() || !!offline}>
              Run the gates
            </button>
          </form>
        </aside>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section>
          <h2 className="mb-2 font-semibold">This session</h2>
          {jobs.length === 0 ? (
            <p className="text-sm text-muted">No jobs yet.</p>
          ) : (
            <ul className="card divide-y divide-line">
              {jobs.map((job) => (
                <li key={job.id}>
                  <Link href={`/job/?id=${job.id}`} className="flex items-center gap-3 px-4 py-3 hover:bg-surface-2">
                    <Pill tone={toneOf(job.status)}>{job.status.replace("_", " ")}</Pill>
                    <span className="min-w-0 flex-1 truncate text-sm">{job.title}</span>
                    {job.verdict ? <VerdictPill verdict={job.verdict} /> : null}
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section>
          <h2 className="mb-2 font-semibold">Previous builds</h2>
          {workspaces.length === 0 ? (
            <p className="text-sm text-muted">Nothing on disk yet.</p>
          ) : (
            <ul className="card divide-y divide-line">
              {workspaces.slice(0, 12).map((w) => (
                <li key={w.path}>
                  <Link
                    href={`/workspace/?path=${encodeURIComponent(w.path)}`}
                    className="flex items-center gap-3 px-4 py-3 hover:bg-surface-2"
                  >
                    <span className="min-w-0 flex-1 truncate font-mono text-xs">{w.name}</span>
                    <VerdictPill verdict={w.verdict} />
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}
