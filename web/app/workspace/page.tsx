"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";

// Old links pointed at /workspace/?path=...; workspaces are projects now.
function Redirect() {
  const router = useRouter();
  const path = useSearchParams().get("path") ?? "";
  useEffect(() => {
    router.replace(path ? `/project/?path=${encodeURIComponent(path)}` : "/");
  }, [router, path]);
  return <p className="text-muted">Opening the project…</p>;
}

export default function WorkspacePage() {
  return (
    <Suspense fallback={null}>
      <Redirect />
    </Suspense>
  );
}
