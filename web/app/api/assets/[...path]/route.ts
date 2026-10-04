import { NextRequest, NextResponse } from "next/server";
import { fetchAsset } from "@/lib/assets";

// 附圖放在私有 bucket，經這裡以 service role 讀出；瀏覽器與 Vercel CDN 快取一週
export async function GET(_req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const params = await ctx.params;
  const rel = params.path.map(decodeURIComponent).join("/");
  if (rel.includes("..")) return new NextResponse("bad path", { status: 400 });
  const r = await fetchAsset(rel);
  if (!r.ok) return new NextResponse("not found", { status: 404 });
  return new NextResponse(r.body, { headers: { "Content-Type": "image/png",
    // 附圖會因校正邊界而重新裁切（路徑不變），快取一天就好
    "Cache-Control": "public, max-age=86400, s-maxage=86400" } });
}
