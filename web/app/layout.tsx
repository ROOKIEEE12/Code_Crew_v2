import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Shell } from "@/components/Shell";

import "./globals.css";

export const metadata: Metadata = {
  title: "CodeCrew",
  description:
    "Check your code before you trust it: tests, branch coverage, mutation testing, security and dependency checks, with a verdict no AI gets to decide.",
};

// Applied before first paint so a saved theme never flashes the other one.
const THEME_SCRIPT = `try{var t=localStorage.getItem("codecrew-theme");if(t==="dark"||t==="light")document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className="min-h-screen">
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
