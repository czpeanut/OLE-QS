/** @type {import('next').NextConfig} */
const nextConfig = {
  // 介面是 public/index.html（單頁、不經 React），首頁直接指過去
  async rewrites() {
    return [{ source: "/", destination: "/index.html" }];
  },
};
export default nextConfig;
