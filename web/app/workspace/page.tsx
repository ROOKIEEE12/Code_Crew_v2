"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { ResultView } from "@/components/ResultView";
import { api, type RunResult } from "@/lib/api";

function WorkspaceView() {
  const path = useSearchParams().get("path") ?? "";
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!path) return;
    api
      .workspace(path)
      .then(setResult)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, [path]);

  return (
    <div className="space-y-4">
      <Link href="/" className="text-sm text-muted hover:text-fg">
        ← All builds
      </Link>
      <h1 className="break-all font-mono text-lg font-bold">{path.split(/[\\/]/).pop()}</h1>
      <p className="text-xs text-muted">
        Loaded from disk: the latest gate report, badges and files. Re-run the gates to refresh it.
      </p>
      {error ? <p className="rounded-md bg-bad-soft px-3 py-2 text-sm text-bad">{error}</p> : null}
      {result ? <ResultView result={result} /> : !error ? <p className="text-muted">Loading…</p> : null}
    </div>
  );
}

export default function WorkspacePage() {
  return (
    <Suspense fallback={<p className="text-muted">Loading…</p>}>
      <WorkspaceView />
    </Suspense>
  );
}
