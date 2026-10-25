"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { Pill } from "@/components/Verdict";
import { api, type BuildRequest, type Health } from "@/lib/api";
import { requestNotifications } from "@/lib/notify";

const EXAMPLES = [
  {
    label: "Password rules",
    text: "A function that validates a password: at least 12 characters, one digit, one uppercase letter, and not one of the 20 most common passwords. Return a list of the rules it breaks (empty if none).",
  },
  {
    label: "Duration parser",
    text: "Parse a duration like '1h30m', '45s' or '2h' into total seconds. Reject anything else (empty string, negative numbers, unknown units) with ValueError.",
  },
  {
    label: "Merge intervals",
    text: "Merge overlapping or touching intervals given as (start, end) tuples and return them sorted by start. start > end raises ValueError; never modify the input.",
  },
];

const MODES: { value: BuildRequest["mode"]; label: string; help: string; badge?: string }[] = [
  {
    value: "multi",
    label: "Multi-agent",
    badge: "recommended",
    help: "A spec you review, then an implementer and a test author who never see each other's work.",
  },
  {
    value: "single",
    label: "Single agent",
    help: "One agent writes code and its own tests, like a typical AI assistant. Same gates judge it.",
  },
  {
    value: "project",
    label: "Project",
    help: "An architect splits the requirement into modules; each is built the multi-agent way.",
  },
];

const STEPS = [
  ["Spec", "The requirement becomes functions, examples and the assumptions the model had to make."],
  ["Your review", "You approve or edit the spec before any code exists. It is the one thing gates cannot check."],
  ["Code + tests", "Written by separate agents from the spec only, so tests are not shaped to fit the code."],
  ["Gates", "Compile, types, dependencies, spec examples, tests, branch coverage, security, mutation testing."],
  ["Repair", "If a gate fails, the failure is diagnosed (code, test or spec) and a patch is kept only if it helps."],
] as const;

export default function BuildPage() {
  const router = useRouter();
  const formRef = useRef<HTMLFormElement>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [requirement, setRequirement] = useState("");
  const [mode, setMode] = useState<BuildRequest["mode"]>("multi");
  const [provider, setProvider] = useState("auto");
  const [script, setScript] = useState("");
  const [repair, setRepair] = useState(2);
  const [approval, setApproval] = useState<"review" | "auto">("review");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .health()
      .then((h) => {
        setHealth(h);
        if (!h.providers.some((p) => p.ready && p.name !== "scripted")) setProvider("scripted");
        if (h.replays.length) setScript(h.replays.includes("clamp_multi.json") ? "clamp_multi.json" : h.replays[0]!);
      })
      .catch(() => undefined);
  }, []);

  async function submit() {
    if (!requirement.trim() || busy) return;
    setBusy(true);
    setError(null);
    requestNotifications();
    try {
      const job = await api.submit({
        kind: "build",
        requirement,
        mode,
        provider,
        repair: mode === "single" ? 0 : repair,
        approval,
        ...(provider === "scripted" ? { script } : {}),
      });
      router.push(`/job/?id=${job.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  const live = health?.providers.filter((p) => p.ready && p.name !== "scripted") ?? [];

  return (
    <div className="space-y-6">
      <header className="max-w-3xl">
        <h1 className="text-2xl font-bold tracking-tight">Build with AI, then prove it works</h1>
        <p className="mt-1 text-muted">
          Describe what you need in plain language. Agents write the code and the tests; deterministic gates decide the
          verdict. No agent gets to say its own work is done.
        </p>
      </header>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
        <form
          ref={formRef}
          className="card space-y-6 p-5"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <div>
            <div className="mb-1 flex items-baseline justify-between gap-2">
              <label htmlFor="req" className="font-semibold">
                What should it do?
              </label>
              <span className="text-xs text-muted">{requirement.length}/8000</span>
            </div>
            <textarea
              id="req"
              className="field h-40 leading-relaxed"
              placeholder="Say what should happen on bad input too. Anything you leave out becomes an assumption you will be asked to check."
              value={requirement}
              onChange={(e) => setRequirement(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                  e.preventDefault();
                  void submit();
                }
              }}
              maxLength={8000}
              required
              aria-describedby="req-help"
            />
            <div id="req-help" className="mt-2 flex flex-wrap items-center gap-2 text-xs">
              <span className="text-muted">Examples:</span>
              {EXAMPLES.map((ex) => (
                <button
                  type="button"
                  key={ex.label}
                  className="rounded-full border border-line px-2.5 py-1 hover:border-accent hover:text-accent"
                  onClick={() => setRequirement(ex.text)}
                >
                  {ex.label}
                </button>
              ))}
            </div>
          </div>

          <fieldset>
            <legend className="mb-2 font-semibold">How should it be built?</legend>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
              {MODES.map((m) => (
                <label
                  key={m.value}
                  className={`cursor-pointer rounded-lg border p-3 text-sm transition-colors ${
                    mode === m.value ? "border-accent bg-accent-soft" : "border-line hover:border-muted"
                  }`}
                >
                  <input
                    type="radio"
                    name="mode"
                    className="sr-only"
                    checked={mode === m.value}
                    onChange={() => setMode(m.value)}
                  />
                  <span className="flex items-center gap-2 font-semibold">
                    {m.label}
                    {m.badge ? <Pill tone="accent">{m.badge}</Pill> : null}
                  </span>
                  <span className="mt-1 block text-xs text-muted">{m.help}</span>
                </label>
              ))}
            </div>
          </fieldset>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <label htmlFor="provider" className="mb-1 block text-sm font-semibold">
                Model
              </label>
              <select id="provider" className="field" value={provider} onChange={(e) => setProvider(e.target.value)}>
                {live.length > 0 ? <option value="auto">Auto (first available free tier)</option> : null}
                {live.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.name}
                  </option>
                ))}
                <option value="scripted">Offline demo (recorded answers)</option>
              </select>
              {live.length === 0 && health ? (
                <p className="mt-1 text-xs text-muted">
                  No model key found. <Link href="/setup/" className="text-accent underline">Set one up</Link> (Groq and
                  Gemini are free), or try the offline demo.
                </p>
              ) : null}
            </div>
            {provider === "scripted" ? (
              <div>
                <label htmlFor="script" className="mb-1 block text-sm font-semibold">
                  Demo
                </label>
                <select id="script" className="field" value={script} onChange={(e) => setScript(e.target.value)}>
                  {(health?.replays ?? []).map((r) => (
                    <option key={r}>{r}</option>
                  ))}
                </select>
                <p className="mt-1 text-xs text-muted">
                  Replays recorded model answers; your text does not change the code. The gates still run for real.
                </p>
              </div>
            ) : null}
            {mode !== "single" ? (
              <>
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
                    <option value="review">I review it before code is written</option>
                    <option value="auto">Skip review (the verdict will say so)</option>
                  </select>
                </div>
                <div>
                  <label htmlFor="repair" className="mb-1 block text-sm font-semibold">
                    Automatic repair
                  </label>
                  <select id="repair" className="field" value={repair} onChange={(e) => setRepair(Number(e.target.value))}>
                    <option value={0}>Off</option>
                    {[1, 2, 3, 4].map((n) => (
                      <option key={n} value={n}>
                        Up to {n} round{n > 1 ? "s" : ""}
                      </option>
                    ))}
                  </select>
                </div>
              </>
            ) : null}
          </div>

          {error ? (
            <p role="alert" className="rounded-md bg-bad-soft px-3 py-2 text-sm text-bad">
              {error}
            </p>
          ) : null}

          <div className="flex flex-wrap items-center gap-3">
            <button className="btn btn-primary px-5 py-2.5" disabled={busy || !requirement.trim()}>
              {busy ? "Starting…" : "Build and verify"}
            </button>
            <span className="text-xs text-muted">
              <kbd>Ctrl</kbd> + <kbd>Enter</kbd>
            </span>
          </div>
        </form>

        <aside className="card h-fit p-5 text-sm">
          <h2 className="mb-3 font-semibold">What happens next</h2>
          <ol className="space-y-3">
            {STEPS.filter(([name]) => mode !== "single" || (name !== "Your review" && name !== "Repair")).map(
              ([name, text], i) => (
                <li key={name} className="flex gap-3">
                  <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-surface-2 text-xs font-bold">
                    {i + 1}
                  </span>
                  <span>
                    <span className="font-medium">{name}</span>
                    <span className="block text-xs text-muted">{text}</span>
                  </span>
                </li>
              ),
            )}
          </ol>
        </aside>
      </div>
    </div>
  );
}
