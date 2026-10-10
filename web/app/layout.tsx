import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "CodeCrew",
  description:
    "AI writes the code; deterministic gates decide whether to trust it. No agent may declare its own success.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <header className="border-b border-line bg-surface">
          <nav className="mx-auto flex max-w-6xl items-center gap-6 px-4 py-3">
            <Link href="/" className="flex items-center gap-2 font-bold tracking-tight">
              <span
                aria-hidden
                className="grid h-7 w-7 place-items-center rounded-md bg-accent text-sm text-white"
              >
                ✓
              </span>
              CodeCrew
            </Link>
            <Link href="/" className="text-sm text-muted hover:text-fg">
              Build
            </Link>
            <Link href="/evaluation/" className="text-sm text-muted hover:text-fg">
              Evaluation
            </Link>
            <span className="ml-auto hidden text-xs text-muted sm:block">
              No agent may declare its own success.
            </span>
          </nav>
        </header>
        <main className="mx-auto max-w-6xl px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
