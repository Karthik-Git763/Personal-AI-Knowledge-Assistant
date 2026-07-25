import type { NextConfig } from "next";
import path from "node:path";

const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: path.join(process.cwd(), ".."),
  experimental: {
    proxyTimeout: 120_000,
  },
  async rewrites() {
    // In Docker, BACKEND_URL is http://backend:3000
    // Locally, it's http://localhost:3000
    const backendHost = process.env.BACKEND_URL || "http://backend:3000";

    return [
      {
        source: "/api/v1/:path*",
        destination: `${backendHost}/api/v1/:path*`,
      },
      {
        source: "/health/:path*",
        destination: `${backendHost}/health/:path*`,
      },
    ];
  },
};

export default nextConfig;
