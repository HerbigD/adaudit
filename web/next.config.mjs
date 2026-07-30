/** @type {import('next').NextConfig} */
const API = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

const nextConfig = {
  reactStrictMode: true,
  // 同源代理：前端一律请求 /api/*，SSE 也走这里，省掉 CORS 与 EventSource 跨域问题
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API}/api/:path*` },
      { source: "/static/:path*", destination: `${API}/static/:path*` },
    ];
  },
};

export default nextConfig;
