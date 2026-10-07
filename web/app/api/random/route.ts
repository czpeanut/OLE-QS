import { NextRequest, NextResponse } from "next/server";
import { draw, type Mix, type Spread } from "@/lib/random";

export const dynamic = "force-dynamic";

// 隨機選題：篩選條件與 /api/questions 相同，另帶題數、分配方式、難度比例、要排除的題目
export async function POST(req: NextRequest) {
  const b = await req.json();
  const list = (v: unknown) => (Array.isArray(v) && v.length ? v.map(String) : undefined);
  const num = (v: unknown) => (v === undefined || v === null || v === "" ? undefined : Number(v));
  try {
    const out = await draw({
      subject: b.subject || undefined, sub: b.sub || undefined, grade: num(b.grade), semester: num(b.semester),
      year: num(b.year), publisher: b.publisher || undefined, units: list(b.units), types: list(b.types),
      answer: list(b.answer), hasFigure: typeof b.has_figure === "boolean" ? b.has_figure : null, q: b.q?.trim() || undefined,
      patterns: list(b.patterns), difficulty: list(b.difficulty)?.map(Number).filter((n) => n >= 1 && n <= 5),
    }, {
      n: Math.max(1, Math.min(Number(b.n) || 10, 100)),
      spread: (["pattern", "unit", "none"].includes(b.spread) ? b.spread : "none") as Spread,
      mix: (["even", "easy", "hard"].includes(b.mix) ? b.mix : "none") as Mix,
      exclude: list(b.exclude) ?? [],
    });
    return NextResponse.json(out);
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
