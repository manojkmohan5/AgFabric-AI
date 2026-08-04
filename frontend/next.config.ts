import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // Everything talks to the FastAPI backend over CORS; no server-side proxying,
  // so the bundle stays static and deployable anywhere.
  env: {},
  // Traces the production dependency graph into .next/standalone, so the
  // container image ships a pruned node_modules and a plain `node server.js`
  // rather than the full workspace plus `next start`.
  output: "standalone",
};

export default config;
