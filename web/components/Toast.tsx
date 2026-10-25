"use client";

import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

type Tone = "ok" | "bad" | "info";
interface Item {
  id: number;
  text: string;
  tone: Tone;
}

const ToastContext = createContext<(text: string, tone?: Tone) => void>(() => {});

export function useToast() {
  return useContext(ToastContext);
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Item[]>([]);
  const push = useCallback((text: string, tone: Tone = "info") => {
    const id = Date.now() + Math.random();
    setItems((prev) => [...prev.slice(-3), { id, text, tone }]);
    setTimeout(() => setItems((prev) => prev.filter((i) => i.id !== id)), 4200);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex max-w-sm flex-col gap-2" role="status" aria-live="polite">
        {items.map((item) => (
          <div
            key={item.id}
            className={`toast pointer-events-auto rounded-lg border px-4 py-3 text-sm shadow-lg ${
              item.tone === "ok"
                ? "border-ok/40 bg-ok-soft text-ok"
                : item.tone === "bad"
                  ? "border-bad/40 bg-bad-soft text-bad"
                  : "border-line bg-surface text-fg"
            }`}
          >
            {item.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
