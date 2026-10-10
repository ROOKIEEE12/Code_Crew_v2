import type { Stage } from "@/lib/progress";

const MARK: Record<Stage["state"], string> = {
  todo: "border-line text-muted",
  active: "border-accent bg-accent text-white pulse",
  done: "border-ok bg-ok text-white",
  skipped: "border-line bg-surface-2 text-muted",
  failed: "border-bad bg-bad text-white",
};

export function Stepper({ stages }: { stages: Stage[] }) {
  return (
    <ol className="flex flex-wrap items-center gap-x-2 gap-y-3" aria-label="Pipeline stages">
      {stages.map((stage, index) => (
        <li key={stage.key} className="flex items-center gap-2">
          <span
            className={`grid h-6 w-6 place-items-center rounded-full border text-xs font-bold ${MARK[stage.state]}`}
            aria-hidden
          >
            {stage.state === "done" ? "✓" : stage.state === "failed" ? "!" : index + 1}
          </span>
          <span className="text-sm">
            <span className={stage.state === "active" ? "font-semibold" : ""}>{stage.label}</span>
            {stage.detail ? <span className="ml-1 text-xs text-muted">({stage.detail})</span> : null}
            <span className="sr-only"> ({stage.state})</span>
          </span>
          {index < stages.length - 1 ? <span className="mx-1 h-px w-6 bg-line" aria-hidden /> : null}
        </li>
      ))}
    </ol>
  );
}
