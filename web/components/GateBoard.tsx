import { GATES, type Board, type BoardState } from "@/lib/progress";

const STYLE: Record<BoardState, { dot: string; label: string }> = {
  PENDING: { dot: "bg-line", label: "waiting" },
  RUNNING: { dot: "bg-accent pulse", label: "running" },
  PASS: { dot: "bg-ok", label: "pass" },
  FAIL: { dot: "bg-bad", label: "fail" },
  ERROR: { dot: "bg-bad", label: "error" },
  SKIP: { dot: "bg-warn", label: "skipped" },
  "N/A": { dot: "bg-surface-2 border border-line", label: "not applicable" },
};

export function GateBoard({ board }: { board: Board }) {
  return (
    <section className="card p-4">
      <div className="mb-3 flex items-baseline justify-between">
        <h2 className="font-semibold">Gates</h2>
        <span className="text-xs text-muted">
          {board.pass > 1 ? `verification pass ${board.pass} (after a repair patch)` : "deterministic, no AI"}
        </span>
      </div>
      <ol className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {board.cells.map((cell) => {
          const gate = GATES.find((g) => g.id === cell.id);
          const style = STYLE[cell.state];
          return (
            <li key={cell.id} className="flex gap-3 rounded-lg border border-line bg-surface-2/50 p-3">
              <span className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${style.dot}`} aria-hidden />
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="font-mono text-xs text-muted">{cell.id}</span>
                  <span className="font-medium">{gate?.name}</span>
                  <span className="text-xs text-muted">
                    {style.label}
                    {cell.blocking === false ? " · advisory" : ""}
                  </span>
                </div>
                <p className="mt-0.5 truncate text-xs text-muted" title={cell.summary ?? gate?.what}>
                  {cell.summary ?? gate?.what}
                </p>
                {cell.note && cell.state === "SKIP" ? (
                  <p className="mt-0.5 text-xs text-warn">{cell.note}</p>
                ) : null}
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
