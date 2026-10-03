import { NextRequest, NextResponse } from "next/server";
import { byIds } from "@/lib/questions";
import { renderPaper } from "@/lib/render";
import { pngWidths } from "@/lib/assets";

export const dynamic = "force-dynamic";

// 不存檔直接排版：題籃只存在瀏覽器，預覽與列印（另存 PDF）都走這裡
export async function POST(req: NextRequest) {
  const body = await req.json();
  const items: { question_id: string; score?: number | null }[] = body.items ?? [];
  if (!items.length) return NextResponse.json({ error: "沒有選任何題目" }, { status: 400 });
  const qs = await byIds(items.map((i) => i.question_id));
  const list = items.filter((i) => qs.has(i.question_id)).map((i) => ({ q: qs.get(i.question_id)!, score: i.score ?? qs.get(i.question_id)!.score }));
  const files = list.flatMap(({ q }) => [...q.assets.map((a) => a.file), ...q.options.map((o) => o.asset_file), q.shared_asset?.file])
    .filter((f): f is string => !!f);
  const html = renderPaper(body.title || "試卷", list, body.mode ?? "exam", body.settings ?? {}, await pngWidths(files));
  return new NextResponse(html, { headers: { "Content-Type": "text/html; charset=utf-8" } });
}
