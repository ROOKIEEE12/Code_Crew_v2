"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { api } from "@/lib/api";

import { ToastProvider } from "./Toast";

const NAV = [
  { href: "/", label: "Projects", icon: "▦", hint: "Check your code" },
  { href: "/build/", label: "Build with AI", icon: "✦", hint: "Generate, then verify" },
  { href: "/runs/", label: "Runs", icon: "↻", hint: "This session" },
  { href: "/evaluation/", label: "Evaluation", icon: "◫", hint: "Research results" },
  { href: "/setup/", label: "Setup", icon: "⚙", hint: "Tools, models, CI" },
];

type Theme = "system" | "light" | "dark";

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  if (theme === "system") delete root.dataset.theme;
  else root.dataset.theme = theme;
}

export function Shell({ children }: { children: ReactNode }) {
  const path = usePathname() ?? "/";
  const [online, setOnline] = useState<boolean | null>(null);
  const [version, setVersion] = useState("");
  const [theme, setTheme] = useState<Theme>("system");

  useEffect(() => {
    try {
      const saved = localStorage.getItem("codecrew-theme") as Theme | null;
      if (saved) {
        setTheme(saved);
        applyTheme(saved);
      }
    } catch {
      /* storage unavailable: follow the system theme */
    }
  }, []);

  useEffect(() => {
    let alive = true;
    const ping = () =>
      api
        .health()
        .then((h) => {
          if (!alive) return;
          setOnline(true);
          setVersion(h.version);
        })
        .catch(() => alive && setOnline(false));
    void ping();
    const timer = setInterval(ping, 15000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  function cycleTheme() {
    const next: Theme = theme === "system" ? "dark" : theme === "dark" ? "light" : "system";
    setTheme(next);
    applyTheme(next);
    try {
      localStorage.setItem("codecrew-theme", next);
    } catch {
      /* ignore */
    }
  }

  const active = (href: string) => (href === "/" ? path === "/" || path.startsWith("/project") : path.startsWith(href));

  return (
    <ToastProvider>
      <a href="#main" className="skip-link">
        Skip to content
      </a>
      <div className="min-h-screen lg:grid lg:grid-cols-[232px_minmax(0,1fr)]">
        <aside className="border-b border-line bg-surface lg:sticky lg:top-0 lg:h-screen lg:border-b-0 lg:border-r">
          <div className="flex items-center gap-2 px-4 py-3 lg:py-5">
            <Link href="/" className="flex items-center gap-2 font-bold tracking-tight">
              <span aria-hidden className="grid h-7 w-7 place-items-center rounded-md bg-accent text-sm text-white">
                ✓
              </span>
              CodeCrew
            </Link>
            <span className="ml-auto flex items-center gap-1.5 text-xs text-muted" title={online === false ? "Start it with `codecrew serve`" : "Local API"}>
              <span
                aria-hidden
                className={`h-2 w-2 rounded-full ${online === null ? "bg-line" : online ? "bg-ok" : "bg-bad"}`}
              />
              {online === null ? "…" : online ? "connected" : "offline"}
            </span>
            <button onClick={cycleTheme} className="btn px-2 py-1 text-xs lg:hidden" aria-label="Change theme">
              {theme === "system" ? "◐" : theme === "dark" ? "●" : "○"}
            </button>
          </div>
          <nav aria-label="Main" className="flex gap-1 overflow-x-auto px-2 pb-2 lg:flex-col lg:overflow-visible lg:px-3">
            {NAV.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active(item.href) ? "page" : undefined}
                className={`flex shrink-0 items-center gap-3 rounded-lg px-3 py-2 text-sm ${
                  active(item.href) ? "bg-accent-soft font-semibold text-accent" : "text-muted hover:bg-surface-2 hover:text-fg"
                }`}
              >
                <span aria-hidden className="w-4 text-center">
                  {item.icon}
                </span>
                <span>
                  {item.label}
                  <span className="hidden text-xs font-normal text-muted lg:block">{item.hint}</span>
                </span>
              </Link>
            ))}
          </nav>
          <div className="hidden px-4 pt-6 text-xs text-muted lg:absolute lg:bottom-4 lg:block lg:w-[232px]">
            <button onClick={cycleTheme} className="btn mb-3 w-full justify-center text-xs" aria-label="Change theme">
              {theme === "system" ? "◐ System theme" : theme === "dark" ? "● Dark" : "○ Light"}
            </button>
            <p>No agent may declare its own success.</p>
            {version ? <p className="mt-1">v{version}</p> : null}
          </div>
        </aside>
        <main id="main" className="mx-auto w-full min-w-0 max-w-6xl px-4 py-6 lg:px-8 lg:py-8">
          {online === false ? (
            <div className="card mb-6 border-bad/40 bg-bad-soft p-4 text-sm text-bad">
              The CodeCrew API is not reachable. Start it in a terminal, then this page reconnects by itself:
              <pre className="code mt-2 px-3 text-fg">codecrew serve</pre>
            </div>
          ) : null}
          {children}
        </main>
      </div>
    </ToastProvider>
  );
}
