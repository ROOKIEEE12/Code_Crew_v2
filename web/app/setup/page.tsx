"use client";

import { useEffect, useState, type ReactNode } from "react";

import { useToast } from "@/components/Toast";
import { Pill } from "@/components/Verdict";
import { api, type Health } from "@/lib/api";

const KEYS: Record<string, { env: string; url: string; note: string }> = {
  groq: { env: "GROQ_API_KEY", url: "https://console.groq.com/keys", note: "free tier, fastest" },
  gemini: { env: "GEMINI_API_KEY", url: "https://aistudio.google.com/apikey", note: "free tier" },
  anthropic: { env: "ANTHROPIC_API_KEY", url: "https://console.anthropic.com/", note: "paid" },
};

export default function SetupPage() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .health()
      .then(setHealth)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  const live = health?.providers.filter((p) => p.ready && p.name !== "scripted") ?? [];

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold tracking-tight">Setup</h1>
        <p className="mt-1 text-muted">What is ready on this machine, and the commands for the rest.</p>
      </header>
      {error ? <p className="rounded-md bg-bad-soft px-3 py-2 text-sm text-bad">{error}</p> : null}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section className="card p-5">
          <h2 className="mb-3 font-semibold">This machine</h2>
          {health ? (
            <ul className="space-y-3 text-sm">
              <Row label="CodeCrew" ok>
                v{health.version}
                {health.python ? ` on Python ${health.python}` : ""}
              </Row>
              <Row label="Gate tools" ok={!!health.tools?.ready}>
                {health.tools?.ready ? (
                  <span className="break-all">installed in {health.tools.path}</span>
                ) : (
                  <>Installed automatically on the first check (about a minute, needs internet once).</>
                )}
              </Row>
              <Row label="Sandbox" ok={health.sandbox.docker} warn={!health.sandbox.docker}>
                {health.sandbox.docker
                  ? "Docker: no network, read-only system, capped CPU and memory."
                  : "Local virtualenv: isolates dependencies but not the machine. Install Docker for real isolation of AI-written code."}
              </Row>
              <Row label="AI model" ok={live.length > 0} warn={live.length === 0}>
                {live.length > 0
                  ? `ready: ${live.map((p) => p.name).join(", ")}`
                  : "none yet. Only needed to build with AI; checking your own code needs no model."}
              </Row>
            </ul>
          ) : (
            <div className="skeleton h-40 w-full" aria-hidden />
          )}
        </section>

        <section className="card p-5 text-sm">
          <h2 className="mb-1 font-semibold">Add a free AI model</h2>
          <p className="mb-3 text-muted">
            Keys are read from environment variables when <code>codecrew serve</code> starts; they are never stored by
            CodeCrew or sent anywhere except the provider. Restart the server after setting one.
          </p>
          <ul className="space-y-3">
            {Object.entries(KEYS).map(([name, k]) => {
              const ready = health?.providers.find((p) => p.name === name)?.ready;
              return (
                <li key={name}>
                  <div className="flex items-center gap-2">
                    <span className="font-medium">{name}</span>
                    <span className="text-xs text-muted">{k.note}</span>
                    {ready ? <Pill tone="ok">ready</Pill> : null}
                    <a href={k.url} target="_blank" rel="noreferrer noopener" className="ml-auto text-xs text-accent hover:underline">
                      get a key ↗
                    </a>
                  </div>
                  <Command text={`$env:${k.env}="..."   # PowerShell`} />
                  <Command text={`export ${k.env}=...   # macOS / Linux`} />
                </li>
              );
            })}
            <li>
              <span className="font-medium">ollama</span> <span className="text-xs text-muted">local, offline, no key</span>
              <Command text="ollama pull qwen2.5-coder:7b" />
            </li>
          </ul>
        </section>
      </div>

      <section className="card p-5 text-sm">
        <h2 className="mb-1 font-semibold">Use it every day</h2>
        <p className="mb-4 text-muted">The same gates, wherever you work. Run these inside your repository.</p>
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
          <Recipe title="Set the repository up" text="Adds [tool.codecrew] to pyproject.toml, ignores .codecrew/, and optionally CI and a pre-commit hook.">
            <Command text="codecrew init --ci --pre-commit" />
          </Recipe>
          <Recipe title="While you work" text="Everything except mutation testing; fast enough to run often.">
            <Command text="codecrew verify . --profile quick" />
          </Recipe>
          <Recipe title="Before you merge" text="Every gate. --strict fails unless the verdict is VERIFIED.">
            <Command text="codecrew verify . --strict" />
          </Recipe>
          <Recipe title="In your editor or code scanning" text="SARIF opens in VS Code's SARIF viewer and in GitHub code scanning.">
            <Command text="codecrew verify . --sarif codecrew.sarif" />
          </Recipe>
        </div>
      </section>
    </div>
  );
}

function Row({ label, ok, warn, children }: { label: string; ok?: boolean; warn?: boolean; children: ReactNode }) {
  return (
    <li className="flex gap-3">
      <span
        aria-hidden
        className={`mt-1 grid h-4 w-4 shrink-0 place-items-center rounded-full text-[9px] font-bold text-white ${
          ok ? "bg-ok" : warn ? "bg-warn" : "bg-line"
        }`}
      >
        {ok ? "✓" : "!"}
      </span>
      <div className="min-w-0">
        <span className="font-medium">{label}</span>
        <span className="sr-only">{ok ? " (ready)" : " (needs attention)"}</span>
        <p className="text-muted">{children}</p>
      </div>
    </li>
  );
}

function Recipe({ title, text, children }: { title: string; text: string; children: ReactNode }) {
  return (
    <div>
      <h3 className="font-medium">{title}</h3>
      <p className="text-xs text-muted">{text}</p>
      {children}
    </div>
  );
}

function Command({ text }: { text: string }) {
  const toast = useToast();
  return (
    <div className="mt-1.5 flex items-center gap-2 rounded-md border border-line bg-code px-3 py-1.5">
      <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap font-mono text-xs">{text}</code>
      <button
        className="shrink-0 text-xs text-muted hover:text-accent"
        aria-label={`Copy: ${text}`}
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(text.split("   #")[0]!.trim());
            toast("Copied.", "ok");
          } catch {
            toast("Your browser blocked the clipboard.", "bad");
          }
        }}
      >
        copy
      </button>
    </div>
  );
}
