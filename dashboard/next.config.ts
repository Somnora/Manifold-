import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Pin the workspace root so Next does not guess from stray lockfiles
  // elsewhere on the machine.
  turbopack: { root: __dirname },
  // Static export: every route already prerenders as static HTML, so the
  // build emits plain files (out/) that the FastAPI backend serves at "/".
  // That is what makes the desktop build a single process - no Node at
  // runtime. `npm run dev` is unaffected.
  output: "export",
};

export default nextConfig;
