import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/supabase";
import { randomBytes } from "crypto";

export const dynamic = "force-dynamic";

export async function GET() {
  const { data, error } = await db().from("paper").select("id,title,subject,updated_at,paper_item(score)").order("updated_at", { ascending: false });
  if (error) return NextResponse.json({ error: error.message }, { status: 500 });
  return NextResponse.json((data ?? []).map((p) => ({ id: p.id, title: p.title, subject: p.subject, updated_at: p.updated_at,
    count: p.paper_item.length, total_score: p.paper_item.reduce((a: number, i: { score: number | null }) => a + (i.score ?? 0), 0) })));
}

export async function POST(req: NextRequest) {
  const b = await req.json();
  const now = new Date().toISOString();
  const id = `paper_${randomBytes(6).toString("hex")}`;
  const { error } = await db().from("paper").insert({ id, title: b.title || "試卷", subject: b.subject ?? null, settings: b.settings ?? null,
                                                     created_at: now, updated_at: now });
  if (error) return NextResponse.json({ error: error.message }, { status: 500 });
  const items = (b.items ?? []).map((it: { question_id: string; score?: number | null }, ord: number) =>
    ({ paper_id: id, question_id: it.question_id, ord, score: it.score ?? null }));
  if (items.length) {
    const r = await db().from("paper_item").insert(items);
    if (r.error) return NextResponse.json({ error: r.error.message }, { status: 500 });
  }
  return NextResponse.json({ id }, { status: 201 });
}
