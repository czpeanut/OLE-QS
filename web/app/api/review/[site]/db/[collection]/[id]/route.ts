import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/supabase";
import { ok } from "../../shared";

export const dynamic = "force-dynamic";
type P = { params: Promise<{ site: string; collection: string; id: string }> };

export async function PUT(req: NextRequest, ctx: P) {
  const params = await ctx.params;
  if (!ok(params.site, params.collection) || params.id.length > 200) return NextResponse.json({ error: "bad path" }, { status: 400 });
  const data = await req.json();
  const { error } = await db().from("review_doc").upsert({ site: params.site, collection: params.collection, id: params.id,
                                                          data, updated_at: new Date().toISOString() });
  if (error) return NextResponse.json({ error: error.message }, { status: 500 });
  return NextResponse.json({ ok: true });
}

export async function DELETE(_req: NextRequest, ctx: P) {
  const params = await ctx.params;
  if (!ok(params.site, params.collection)) return NextResponse.json({ error: "bad path" }, { status: 400 });
  const { error } = await db().from("review_doc").delete().match({ site: params.site, collection: params.collection, id: params.id });
  if (error) return NextResponse.json({ error: error.message }, { status: 500 });
  return new NextResponse(null, { status: 204 });
}
