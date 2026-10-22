"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Findings } from "@/components/Findings";
import { useToast } from "@/components/Toast";
import { TrendChart, VerdictDots } from "@/components/Trend";
import { Pill, VerdictBanner, VerdictPill, toneOf } from "@/components/Verdict";
import { projectsApi, type FileBadge, type GateResult, type ProjectDetail } from "@/lib/api";
import { timeAgo } from "@/lib/fixprompt";
import { requestNotifications } from "@/lib/notify";
import { GATES } from "@/lib/progress";

type Tab = "overview" | "findings" | "files" | "history" | "settings";

export function ProjectView() {
  const params = useSearchParams();
  const router = useRouter();
  const toast = useToast();
  const path = params.get("path") ?? "";
  const run = params.get("run") ?? undefined;
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const initialTab = params.get("tab") as Tab | null;
  const [tab, setTabState] = useState<Tab>(
    initialTab && ["overview", "findings", "files", "history", "settings"].includes(initialTab) ? initialTab : "overview",
  );
  // The tab is part of the address, so a link can point at a project's findings.
  const setTab = (next: Tab) => {
    setTabState(next);
    const query = new URLSearchParams(params.toString());
    if (next === "overview") query.delete("tab");
    else query.set("tab", next);
    router.replace(`/project/?${query.toString()}`, { scroll: false });
  };
  const [starting, setStarting] = useState(false);

  const load = useCallback(() => {
    if (!path) return;
    projectsApi
      .detail(path, run)
      .then((d) => {
        setDetail(d);
        setError(null);
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, [path, run]);

  useEffect(() => {
    setDetail(null);
    load();
  }, [load]);

  async function check(profile: "quick" | "full") {
    setStarting(true);
    requestNotifications();
    try {
      const job = await projectsApi.verify(path, profile);
      router.push(`/job/?id=${job.id}`);
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "bad");
      setStarting(false);
    }
  }

  async function remove() {
    if (!detail) return;
    try {
      await projectsApi.remove(detail.path);
      toast(`Removed ${detail.name} from the list. Nothing on disk was touched.`, "ok");
      router.push("/");
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "bad");
    }
  }

  if (!path) return <p className="text-muted">No project selected.</p>;
  if (error) {
    return (
      <div className="space-y-4">
        <Link href="/" className="text-sm text-muted hover:text-fg">
          ← Projects
        </Link>
        <p className="rounded-md bg-bad-soft px-3 py-2 text-sm text-bad">{error}</p>
      </div>
    );
  }
  if (!detail) return <DetailSkeleton />;

  const report = detail.report;
  const problems = detail.findings.filter((f) => ["critical", "high", "medium"].includes(f.severity)).length;
  const tabs: { key: Tab; label: string }[] = [
    { key: "overview", label: "Overview" },
    { key: "findings", label: `Findings${problems ? ` (${problems})` : ""}` },
    { key: "files", label: `Files${detail.badges.length ? ` (${detail.badges.length})` : ""}` },
    { key: "history", label: `History (${detail.history.length})` },
    { key: "settings", label: "Settings" },
  ];
  const isLatest = !run || detail.history[0]?.run_id === run;

  return (
    <div className="space-y-6">
      <div>
        <Link href="/" className="text-sm text-muted hover:text-fg">
          ← Projects
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <div className="min-w-0 flex-1">
            <h1 className="flex flex-wrap items-center gap-2 text-2xl font-bold tracking-tight">
              {detail.name}
              {detail.kind === "generated" ? <Pill tone="accent">AI build</Pill> : null}
            </h1>
            <p className="truncate font-mono text-xs text-muted" title={detail.path}>
              {detail.path}
            </p>
          </div>
          <button className="btn" disabled={starting} onClick={() => check("quick")}
            title="Everything except mutation testing. Fast; can never say VERIFIED.">
            Quick check
          </button>
          <button className="btn btn-primary" disabled={starting} onClick={() => check("full")}>
            {starting ? "Starting…" : "Full check"}
          </button>
        </div>
      </div>

      {!report ? (
        <div className="card p-6 text-sm">
          <p className="font-semibold">This project has not been checked yet.</p>
          <p className="mt-1 text-muted">
            Run a quick check to see where it stands. From a terminal you can run the same thing with{" "}
            <code>codecrew verify &quot;{detail.path}&quot;</code>; the result shows up here either way.
          </p>
        </div>
      ) : (
        <>
          {!isLatest ? (
            <p className="rounded-md bg-accent-soft px-3 py-2 text-sm text-accent">
              Showing an older run ({timeAgo(detail.history.find((h) => h.run_id === run)?.time ?? 0)}).{" "}
              <Link href={`/project/?path=${encodeURIComponent(path)}`} className="underline">
                Back to the latest
              </Link>
            </p>
          ) : null}
          <VerdictBanner verdict={report.verdict} note={detail.verdict_note} />

          <div role="tablist" aria-label="Project sections" className="flex flex-wrap gap-1 border-b border-line">
            {tabs.map((t) => (
              <button
                key={t.key}
                role="tab"
                aria-selected={tab === t.key}
                onClick={() => setTab(t.key)}
                className={`-mb-px border-b-2 px-3 py-2 text-sm ${
                  tab === t.key ? "border-accent font-semibold" : "border-transparent text-muted hover:text-fg"
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>

          <div role="tabpanel">
            {tab === "overview" ? <Overview detail={detail} onFindings={() => setTab("findings")} /> : null}
            {tab === "findings" ? (
              <Findings findings={detail.findings} project={detail.name} verdict={report.verdict} />
            ) : null}
            {tab === "files" ? <Files path={detail.path} badges={detail.badges} detail={detail} /> : null}
            {tab === "history" ? <History detail={detail} current={run ?? detail.history[0]?.run_id} /> : null}
            {tab === "settings" ? <Settings detail={detail} onRemove={remove} /> : null}
          </div>
        </>
      )}
      {!report && detail.registered ? (
        <button className="text-xs text-muted underline" onClick={remove}>
          Remove from the list
        </button>
      ) : null}
    </div>
  );
}

function Overview({ detail, onFindings }: { detail: ProjectDetail; onFindings: () => void }) {
  const report = detail.report!;
  const latest = detail.history[0];
  const trend = [...detail.history].reverse().slice(-15);
  const settings = (detail.info.settings ?? {}) as Record<string, number>;
  const results = new Map(report.results.map((r) => [r.gate_id, r]));
  const blocked = report.results.filter((r) => r.blocking && (r.status === "FAIL" || r.status === "ERROR"));

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <section className="card p-4">
        <h2 className="mb-3 font-semibold">Gates</h2>
        {blocked.length ? (
          <p className="mb-3 rounded-md bg-bad-soft px-3 py-2 text-sm text-bad">
            {blocked.map((b) => b.gate_id).join(", ")} failed.{" "}
            <button onClick={onFindings} className="underline">
              See the findings
            </button>
          </p>
        ) : null}
        <ul className="space-y-1.5">
          {GATES.map((g) => {
            const r = results.get(g.id);
            return <GateRow key={g.id} id={g.id} name={g.name} what={g.what} result={r} />;
          })}
        </ul>
      </section>

      <div className="space-y-6">
        <section className="card p-4">
          <div className="mb-3 flex items-baseline justify-between">
            <h2 className="font-semibold">Trend</h2>
            <VerdictDots runs={trend} size="md" />
          </div>
          {trend.length < 2 ? (
            <p className="text-sm text-muted">Run it again later to see a trend here.</p>
          ) : (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <TrendChart runs={trend} pick={(r) => r.coverage} title="Branch coverage" threshold={settings["min-coverage"] ?? 70} />
              <TrendChart runs={trend} pick={(r) => r.mutation} title="Mutation score" threshold={settings["min-mutation"] ?? 50} />
            </div>
          )}
        </section>

        <section className="card p-4 text-sm">
          <h2 className="mb-2 font-semibold">This run</h2>
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
            {latest ? (
              <>
                <dt className="text-muted">when</dt>
                <dd>{timeAgo(latest.time)}</dd>
                <dt className="text-muted">profile</dt>
                <dd>{latest.profile === "quick" ? "quick (no mutation testing)" : "full"}</dd>
                <dt className="text-muted">took</dt>
                <dd>{Math.round(report.duration_s)} s</dd>
              </>
            ) : null}
            <dt className="text-muted">sandbox</dt>
            <dd>
              {report.sandbox}
              {report.sandbox === "local-venv" ? (
                <span className="ml-1 text-xs text-warn">(isolates dependencies, not the machine)</span>
              ) : null}
            </dd>
          </dl>
          {report.notes?.length ? (
            <ul className="mt-3 space-y-1 border-t border-line pt-3 text-xs text-muted">
              {report.notes.map((n) => (
                <li key={n}>• {n}</li>
              ))}
            </ul>
          ) : null}
        </section>
      </div>
    </div>
  );
}

function GateRow({ id, name, what, result }: { id: string; name: string; what: string; result?: GateResult }) {
  const status = result?.status ?? "N/A";
  const tone = result ? toneOf(result.status) : "muted";
  return (
    <li className="flex items-start gap-3 rounded-lg px-2 py-1.5 hover:bg-surface-2/60">
      <Pill tone={tone}>{status === "N/A" ? "n/a" : status.toLowerCase()}</Pill>
      <div className="min-w-0 flex-1 text-sm">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="font-mono text-xs text-muted">{id}</span>
          <span className="font-medium">{name}</span>
          {result && !result.blocking ? <span className="text-xs text-muted">advisory</span> : null}
        </div>
        <p className="truncate text-xs text-muted" title={result?.note ?? result?.summary ?? what}>
          {result ? result.summary + (result.note ? ` · ${result.note}` : "") : `did not apply · ${what}`}
        </p>
      </div>
    </li>
  );
}

function Files({ path, badges, detail }: { path: string; badges: FileBadge[]; detail: ProjectDetail }) {
  const [open, setOpen] = useState<string | null>(badges[0]?.path ?? null);
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setContent(null);
    setError(null);
    projectsApi
      .file(path, open)
      .then((f) => setContent(f.content))
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, [path, open]);

  const marks = useMemo(() => {
    const out = new Map<number, string>();
    for (const f of detail.findings) {
      if (f.file === open && f.line && ["critical", "high", "medium"].includes(f.severity)) {
        out.set(f.line, `${f.gate}: ${f.message}`);
      }
    }
    return out;
  }, [detail.findings, open]);

  if (badges.length === 0) return <p className="text-sm text-muted">No source files were found in the last run.</p>;
  const badge = badges.find((b) => b.path === open);

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-[280px_minmax(0,1fr)]">
      <ul className="max-h-[70vh] space-y-0.5 overflow-y-auto">
        {badges.map((b) => (
          <li key={b.path}>
            <button
              onClick={() => setOpen(b.path)}
              aria-current={open === b.path ? "true" : undefined}
              className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left ${open === b.path ? "bg-accent-soft" : "hover:bg-surface-2"}`}
            >
              <span className={`h-2 w-2 shrink-0 rounded-full ${b.badge === "VERIFIED" ? "bg-ok" : b.badge === "UNVERIFIED" ? "bg-bad" : "bg-warn"}`} aria-hidden />
              <span className="min-w-0 flex-1 truncate font-mono text-xs">{b.path}</span>
              <span className="sr-only">{b.badge}</span>
            </button>
          </li>
        ))}
      </ul>
      <div className="min-w-0">
        {badge ? (
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm">{badge.path}</span>
            <VerdictPill verdict={badge.badge} />
          </div>
        ) : null}
        {badge && badge.reasons.length ? (
          <ul className="mb-2 space-y-0.5 text-xs text-warn">
            {badge.reasons.slice(0, 4).map((r) => (
              <li key={r}>• {r}</li>
            ))}
          </ul>
        ) : null}
        {error ? <p className="text-sm text-bad">{error}</p> : null}
        {content === null && !error ? <div className="skeleton h-64 w-full" /> : null}
        {content !== null ? (
          <pre className="code max-h-[70vh] overflow-y-auto">
            {content.replace(/\n$/, "").split("\n").map((line, i) => {
              const mark = marks.get(i + 1);
              return (
                <div key={i} className={`flex ${mark ? "bg-warn-soft" : ""}`} title={mark}>
                  <span className="w-12 shrink-0 select-none pr-3 text-right text-muted/70">{i + 1}</span>
                  <code className="whitespace-pre pr-4">{line || " "}</code>
                </div>
              );
            })}
          </pre>
        ) : null}
      </div>
    </div>
  );
}

function History({ detail, current }: { detail: ProjectDetail; current?: string }) {
  const base = `/project/?path=${encodeURIComponent(detail.path)}`;
  return (
    <div className="card overflow-x-auto">
      <table className="w-full min-w-[640px] text-sm">
        <thead>
          <tr className="border-b border-line text-left text-xs text-muted">
            <th className="px-4 py-2">When</th>
            <th className="pr-3">Verdict</th>
            <th className="pr-3">Profile</th>
            <th className="pr-3 text-right">Tests</th>
            <th className="pr-3 text-right">Coverage</th>
            <th className="pr-3 text-right">Mutation</th>
            <th className="pr-4 text-right">Took</th>
          </tr>
        </thead>
        <tbody>
          {detail.history.map((h) => (
            <tr key={h.run_id} className={`border-b border-line/60 ${h.run_id === current ? "bg-accent-soft/60" : ""}`}>
              <td className="px-4 py-2">
                <Link href={`${base}&run=${encodeURIComponent(h.run_id)}`} className="hover:text-accent hover:underline">
                  {timeAgo(h.time)}
                </Link>
                <span className="block text-xs text-muted">{new Date(h.time * 1000).toLocaleString()}</span>
              </td>
              <td className="pr-3">
                <VerdictPill verdict={h.verdict} />
              </td>
              <td className="pr-3 text-muted">{h.profile}</td>
              <td className="pr-3 text-right tabular-nums">{h.tests ?? "–"}</td>
              <td className="pr-3 text-right tabular-nums">{h.coverage === null ? "–" : `${h.coverage.toFixed(0)}%`}</td>
              <td className="pr-3 text-right tabular-nums">{h.mutation === null ? "–" : `${h.mutation.toFixed(0)}%`}</td>
              <td className="pr-4 text-right tabular-nums text-muted">{Math.round(h.duration_s)} s</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Settings({ detail, onRemove }: { detail: ProjectDetail; onRemove: () => void }) {
  const info = detail.info;
  const settings = info.settings ?? {};
  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
      <section className="card p-4 text-sm">
        <h2 className="mb-2 font-semibold">Effective configuration</h2>
        {info.config_error ? (
          <p className="mb-3 rounded-md bg-bad-soft px-3 py-2 text-bad">{info.config_error}</p>
        ) : null}
        <p className="mb-3 text-muted">
          From {info.settings_file ? <code>{info.settings_file}</code> : "built-in defaults (no [tool.codecrew] found)"}.
        </p>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
          <dt className="text-muted">profile</dt>
          <dd>{String(settings.profile ?? "full")}</dd>
          <dt className="text-muted">min coverage</dt>
          <dd>{String(settings["min-coverage"] ?? 70)}%</dd>
          <dt className="text-muted">min mutation</dt>
          <dd>{String(settings["min-mutation"] ?? 50)}%</dd>
          <dt className="text-muted">layout</dt>
          <dd>{info.source_root ? `src layout (${info.source_root}/)` : "flat"}</dd>
          <dt className="text-muted">dependencies</dt>
          <dd>
            {info.dependencies ?? 0} declared
            {info.requirement_files?.length ? `, ${info.requirement_files.join(", ")}` : ""}
            {info.install_dependencies === false ? " (not installed automatically)" : ""}
          </dd>
          {Array.isArray(settings.disable) && settings.disable.length ? (
            <>
              <dt className="text-muted">disabled</dt>
              <dd>{(settings.disable as string[]).join(", ")}</dd>
            </>
          ) : null}
          {Array.isArray(settings.exclude) && settings.exclude.length ? (
            <>
              <dt className="text-muted">excluded</dt>
              <dd className="font-mono text-xs">{(settings.exclude as string[]).join(", ")}</dd>
            </>
          ) : null}
        </dl>
      </section>
      <section className="card p-4 text-sm">
        <h2 className="mb-2 font-semibold">Change it</h2>
        <p className="text-muted">
          Settings live in your repository, so CI and teammates use the same ones. Add or edit this in{" "}
          <code>pyproject.toml</code>:
        </p>
        <pre className="code mt-2 px-3">{`[tool.codecrew]
profile = "full"          # or "quick"
min-coverage = 70
min-mutation = 50
exclude = ["migrations/*"]`}</pre>
        <p className="mt-3 text-muted">
          Or let CodeCrew write it, plus CI and a pre-commit hook: <code>codecrew init --ci --pre-commit</code>
        </p>
        {detail.registered ? (
          <button className="btn mt-4 text-bad" onClick={onRemove}>
            Remove from CodeCrew
          </button>
        ) : null}
      </section>
    </div>
  );
}

function DetailSkeleton() {
  return (
    <div className="space-y-4" aria-hidden>
      <div className="skeleton h-8 w-1/3" />
      <div className="skeleton h-24 w-full" />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="skeleton h-72" />
        <div className="skeleton h-72" />
      </div>
    </div>
  );
}
