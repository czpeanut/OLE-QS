import { NextResponse } from "next/server";
import { tree } from "@/lib/taxonomy";

export function GET() {
  return NextResponse.json(tree());
}
