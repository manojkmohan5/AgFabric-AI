import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // Everything talks to the FastAPI backend over CORS; no server-side proxying,
  // so the bundle stays static and deployable anywhere.
  env: {},
};

export default config;
