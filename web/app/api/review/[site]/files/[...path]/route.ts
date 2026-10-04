import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/supabase";

export const dynamic = "force-dynamic";
const SITES = new Set(["7-1", "7-2", "8-1", "8-2", "9-1", "9-2"]);

// 審題台的題目資料（每檔最大十幾 MB，超過 Vercel 函式的回應上限）：
// 不經過這裡轉送，改成轉址到 Supabase Storage 的限時下載網址，瀏覽器直接去拿
export async function GET(_req: NextRequest, ctx: { params: Promise<{ site: string; path: string[] }> }) {
  const params = await ctx.params;
  const rel = params.path.join("/");
  if (!SITES.has(params.site) || rel.includes("..")) return new NextResponse("bad path", { status: 400 });
  const { data, error } = await db().storage.from("review").createSignedUrl(`${params.site}/${rel}`, 3600);
  if (error || !data) return new NextResponse("not found", { status: 404 });
  return NextResponse.redirect(data.signedUrl, 302);
}
