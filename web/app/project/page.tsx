import { Suspense } from "react";

import { ProjectView } from "./ProjectView";

export default function ProjectPage() {
  return (
    <Suspense fallback={<p className="text-muted">Loading…</p>}>
      <ProjectView />
    </Suspense>
  );
}
