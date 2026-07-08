/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Served at crucible.nousergon.ai/dash via the crucible-live-proxy Worker →
  // origin nginx → :3002 (same path-proxy pattern as the Streamlit skin it
  // replaces at 9-D cutover; mirrors telos-web's basePath=/dash on :3001).
  basePath: "/dash",
};

export default nextConfig;
