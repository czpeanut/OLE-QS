import { NextRequest, NextResponse } from "next/server";
import { units } from "@/lib/taxonomy";

export function GET(req: NextRequest) {
  const p = req.nextUrl.searchParams;
  return NextResponse.json(units(p.get("subject") ?? "", p.get("sub") || null, Number(p.get("grade")),
                                 Number(p.get("semester")), p.get("publisher") || null));
}
