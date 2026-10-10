import type { NextConfig } from "next";

// Built as a static site and served by `codecrew serve` from the same
// origin as the API, so production needs no CORS and no Node server.
// During `next dev` the API runs separately on :8765 (see lib/api.ts).
const config: NextConfig = {
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
  reactStrictMode: true,
  poweredByHeader: false,
};

export default config;
