import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  poweredByHeader: false,
  // Step 39 addition only: emits a minimal self-contained server
  // bundle (.next/standalone) for the Docker image, so the runtime
  // stage doesn't need to ship full node_modules. This is a BUILD
  // OUTPUT setting only -- it changes nothing about request handling,
  // routing, auth, or the existing headers below.
  output: 'standalone',
  async headers() {
    return [{
      source: '/:path*',
      headers: [
        { key: 'X-Content-Type-Options', value: 'nosniff' },
        { key: 'X-Frame-Options', value: 'DENY' },
        { key: 'Referrer-Policy', value: 'no-referrer' },
      ],
    }];
  },
};

export default nextConfig;
