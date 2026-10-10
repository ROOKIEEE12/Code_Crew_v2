import { Suspense } from "react";

import { JobView } from "./JobView";

export default function JobPage() {
  return (
    <Suspense fallback={<p className="text-muted">Loading…</p>}>
      <JobView />
    </Suspense>
  );
}
