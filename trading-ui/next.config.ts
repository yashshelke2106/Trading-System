import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Next 16: Turbopack default. Explicit for clarity.
  experimental: {
    // Compile only routes user actually visits (saves dev mem + cycles)
    serverComponentsHmrCache: true,
  },
  // React strict mode = double-renders in dev (catches bugs but slower).
  // Disable if dev feels sluggish.
  reactStrictMode: false,
  // Skip source maps in dev for faster compile.
  productionBrowserSourceMaps: false,
};

export default nextConfig;
