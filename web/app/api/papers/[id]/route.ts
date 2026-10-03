import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/supabase";
import { byIds } from "@/lib/questions";

export const dynamic = "force-dynamic";

export async function GET(_req: NextRequest, { params }: { params: { id: string } }) {
  const { data: p, error } = await db().from("paper").select("id,title,subject,settings,paper_item(question_id,ord,score)").eq("id", params.id).single();
  if (error || !p) return NextResponse.json({ error: "找不到試卷" }, { status: 404 });
  const items = [...p.paper_item].sort((a, b) => a.ord - b.ord);
  const qs = await byIds(items.map((i) => i.question_id));
  return NextResponse.json({ id: p.id, title: p.title, subject: p.subject, settings: p.settings ?? {},
    items: items.filter((i) => qs.has(i.question_id)).map((i) => ({ score: i.score, question: qs.get(i.question_id) })) });
}

export async function DELETE(_req: NextRequest, { params }: { params: { id: string } }) {
  await db().from("paper").delete().eq("id", params.id);      // paper_item 隨之級聯刪除
  return new NextResponse(null, { status: 204 });
}
