import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/supabase";
import { ok } from "../shared";

export const dynamic = "force-dynamic";

// 一個集合的全部紀錄（回報、審完、邊緣調整、審題者名字）
export async function GET(_req: NextRequest, ctx: { params: Promise<{ site: string; collection: string }> }) {
  const params = await ctx.params;
  if (!ok(params.site, params.collection)) return NextResponse.json({ error: "bad path" }, { status: 400 });
  const out: { id: string; data: unknown }[] = [];
  for (let from = 0; ; from += 1000) {
    const { data, error } = await db().from("review_doc").select("id,data")
      .eq("site", params.site).eq("collection", params.collection).range(from, from + 999);
    if (error) return NextResponse.json({ error: error.message }, { status: 500 });
    out.push(...(data ?? []));
    if ((data ?? []).length < 1000) break;
  }
  return NextResponse.json(out);
}
