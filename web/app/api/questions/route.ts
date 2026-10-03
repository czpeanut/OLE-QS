import { NextRequest, NextResponse } from "next/server";
import { search } from "@/lib/questions";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const p = req.nextUrl.searchParams;
  const list = (k: string) => (p.get(k) ? p.get(k)!.split(",").filter(Boolean) : undefined);
  const num = (k: string) => (p.get(k) ? Number(p.get(k)) : undefined);
  const fig = p.get("has_figure");
  try {
    return NextResponse.json(await search({
      subject: p.get("subject") || undefined, sub: p.get("sub") || undefined, grade: num("grade"), semester: num("semester"),
      year: num("year"), publisher: p.get("publisher") || undefined, units: list("units"), types: list("type"),
      answer: list("answer"), hasFigure: fig === null ? null : fig === "true", q: p.get("q")?.trim() || undefined,
      limit: Math.min(num("limit") ?? 30, 200), offset: num("offset") ?? 0,
    }));
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
