"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { useToast } from "@/components/Toast";
import { VerdictDots } from "@/components/Trend";
import { Pill, VerdictPill } from "@/components/Verdict";
import { projectsApi, type ProjectSummary } from "@/lib/api";
import { timeAgo } from "@/lib/fixprompt";
import { requestNotifications } from "@/lib/notify";

export default function ProjectsPage() {
  const router = useRouter();
  const toast = useToast();
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [path, setPath] = useState("");
  const [adding, setAdding] = useState(false);
  const [starting, setStarting] = useState<string | null>(null);

  const load = useCallback(() => {
    projectsApi
      .list()
      .then((r) => {
        setProjects(r.projects);
        setError(null);
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, [load]);

  async function add() {
    if (!path.trim()) return;
    setAdding(true);
    try {
      const project = await projectsApi.add(path.trim());
      toast(`Added ${project.name}.`, "ok");
      setPath("");
      load();
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "bad");
    } finally {
      setAdding(false);
    }
  }

  async function check(project: ProjectSummary, profile: "quick" | "full") {
    setStarting(`${project.path}:${profile}`);
    requestNotifications();
    try {
      const job = await projectsApi.verify(project.path, profile);
      router.push(`/job/?id=${job.id}`);
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "bad");
      setStarting(null);
    }
  }

  const mine = projects?.filter((p) => p.registered) ?? [];
  const builds = projects?.filter((p) => !p.registered) ?? [];

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-end gap-4">
        <div className="min-w-0 flex-1">
          <h1 className="text-2xl font-bold tracking-tight">Projects</h1>
          <p className="mt-1 text-muted">
            Check a codebase with real tests, branch coverage, mutation testing, security and dependency checks. The
            verdict comes from the tools, not from an AI.
          </p>
        </div>
        <form
          className="flex w-full gap-2 sm:w-auto"
          onSubmit={(e) => {
            e.preventDefault();
            void add();
          }}
        >
          <label htmlFor="add-path" className="sr-only">
            Project folder
          </label>
          <input
            id="add-path"
            className="field font-mono text-xs sm:w-80"
            placeholder={"C:\\code\\my-project  or  /home/me/my-project"}
            value={path}
            onChange={(e) => setPath(e.target.value)}
          />
          <button className="btn btn-primary shrink-0" disabled={adding || !path.trim()}>
            {adding ? "Adding…" : "Add project"}
          </button>
        </form>
      </header>

      {error ? <p className="rounded-md bg-bad-soft px-3 py-2 text-sm text-bad">{error}</p> : null}

      {projects === null && !error ? <CardSkeletons /> : null}

      {projects !== null && mine.length === 0 ? <Welcome /> : null}

      {mine.length > 0 ? (
        <section aria-labelledby="mine">
          <h2 id="mine" className="sr-only">
            Your projects
          </h2>
          <ul className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            {mine.map((p) => (
              <ProjectCard key={p.path} project={p} onCheck={check} starting={starting} />
            ))}
          </ul>
        </section>
      ) : null}

      {builds.length > 0 ? (
        <section aria-labelledby="builds" className="space-y-3">
          <div className="flex items-baseline justify-between">
            <h2 id="builds" className="font-semibold">
              AI builds
            </h2>
            <Link href="/build/" className="text-sm text-accent hover:underline">
              New build →
            </Link>
          </div>
          <ul className="card divide-y divide-line">
            {builds.slice(0, 15).map((b) => (
              <li key={b.path}>
                <Link
                  href={`/project/?path=${encodeURIComponent(b.path)}`}
                  className="flex flex-wrap items-center gap-3 px-4 py-3 hover:bg-surface-2"
                >
                  <span className="min-w-0 flex-1 truncate font-mono text-xs">{b.name}</span>
                  <VerdictDots runs={b.history.slice(-6)} />
                  <VerdictPill verdict={b.latest?.verdict} />
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

function ProjectCard({
  project,
  onCheck,
  starting,
}: {
  project: ProjectSummary;
  onCheck: (p: ProjectSummary, profile: "quick" | "full") => void;
  starting: string | null;
}) {
  const latest = project.latest;
  const href = `/project/?path=${encodeURIComponent(project.path)}`;
  return (
    <li className="card flex flex-col p-4">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <Link href={href} className="font-semibold hover:text-accent hover:underline">
            {project.name}
          </Link>
          <p className="truncate font-mono text-xs text-muted" title={project.path}>
            {project.path}
          </p>
        </div>
        {!project.exists ? <Pill tone="bad">folder missing</Pill> : <VerdictPill verdict={latest?.verdict} />}
      </div>

      {latest ? (
        <>
          <dl className="mt-4 grid grid-cols-3 gap-2 text-center">
            <Stat label="tests" value={latest.tests === null ? "–" : String(latest.tests)} />
            <Stat label="coverage" value={latest.coverage === null ? "–" : `${latest.coverage.toFixed(0)}%`} />
            <Stat label="mutation" value={latest.mutation === null ? "–" : `${latest.mutation.toFixed(0)}%`} />
          </dl>
          <div className="mt-3 flex items-center justify-between gap-2 text-xs text-muted">
            <VerdictDots runs={project.history.slice(-8)} />
            <span>
              {timeAgo(latest.time)} · {latest.profile}
            </span>
          </div>
        </>
      ) : (
        <p className="mt-4 text-sm text-muted">Never checked. Start with a quick check; it takes about a minute.</p>
      )}

      <div className="mt-4 flex gap-2 pt-1">
        <button
          className="btn flex-1 justify-center"
          disabled={!project.exists || starting !== null}
          onClick={() => onCheck(project, "quick")}
          title="Everything except mutation testing. Fast; can never say VERIFIED."
        >
          {starting === `${project.path}:quick` ? "Starting…" : "Quick check"}
        </button>
        <button
          className="btn btn-primary flex-1 justify-center"
          disabled={!project.exists || starting !== null}
          onClick={() => onCheck(project, "full")}
          title="Every gate, mutation testing included."
        >
          {starting === `${project.path}:full` ? "Starting…" : "Full check"}
        </button>
      </div>
    </li>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-surface-2/70 px-2 py-2">
      <dt className="text-[11px] uppercase tracking-wide text-muted">{label}</dt>
      <dd className="text-lg font-semibold tabular-nums">{value}</dd>
    </div>
  );
}

function Welcome() {
  return (
    <section className="card grid grid-cols-1 gap-6 p-6 md:grid-cols-3">
      <div className="md:col-span-3">
        <h2 className="text-lg font-semibold">Start in under a minute</h2>
        <p className="text-sm text-muted">Pick whichever fits what you are doing today.</p>
      </div>
      <Step n={1} title="Check code you already have">
        Paste your project&apos;s folder above and press <strong>Add project</strong>, then <strong>Quick check</strong>.
        Python projects with tests work best; dependencies in <code>pyproject.toml</code> or{" "}
        <code>requirements.txt</code> are installed in an isolated environment.
      </Step>
      <Step n={2} title="Build something new with AI">
        <Link href="/build/" className="text-accent underline">
          Describe it
        </Link>{" "}
        in plain language. You review the spec, separate agents write code and tests, and the gates decide. There is an
        offline demo if you have no model key yet.
      </Step>
      <Step n={3} title="Keep it checked">
        Run <code>codecrew init --ci --pre-commit</code> in your repository so every commit and pull request runs the
        same gates. See{" "}
        <Link href="/setup/" className="text-accent underline">
          Setup
        </Link>
        .
      </Step>
    </section>
  );
}

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-3 text-sm">
      <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-accent-soft font-bold text-accent">{n}</span>
      <div>
        <h3 className="font-semibold">{title}</h3>
        <p className="mt-1 text-muted">{children}</p>
      </div>
    </div>
  );
}

function CardSkeletons() {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3" aria-hidden>
      {[0, 1, 2].map((i) => (
        <div key={i} className="card space-y-3 p-4">
          <div className="skeleton h-5 w-1/2" />
          <div className="skeleton h-3 w-3/4" />
          <div className="skeleton h-16 w-full" />
          <div className="skeleton h-9 w-full" />
        </div>
      ))}
    </div>
  );
}
