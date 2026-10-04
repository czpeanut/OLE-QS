/** @type {import('next').NextConfig} */
const nextConfig = {
  // 介面是 public/index.html（單頁、不經 React），首頁直接指過去
  async rewrites() {
    return [
      { source: "/", destination: "/index.html" },
      // 審題台：/review 是六站選單，/review/7-1 這類網址都用同一份頁面，站別由網址判斷
      { source: "/review", destination: "/review-home.html" },
      { source: "/review/:site(7-1|7-2|8-1|8-2|9-1|9-2)", destination: "/review.html" },
    ];
  },
};
export default nextConfig;
