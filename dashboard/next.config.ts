import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Pin the workspace root so Next does not guess from stray lockfiles
  // elsewhere on the machine.
  turbopack: { root: __dirname },
};

export default nextConfig;
