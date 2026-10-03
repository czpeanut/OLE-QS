import { NextRequest, NextResponse } from "next/server";

// 有設 SITE_ACCESS_CODE 時，整站要先輸入通行碼（題庫內容不公開）
async function sha256(s: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function middleware(req: NextRequest) {
  const code = process.env.SITE_ACCESS_CODE;
  if (!code) return NextResponse.next();
  const { pathname } = req.nextUrl;
  if (pathname === "/login.html" || pathname === "/api/login") return NextResponse.next();
  if (req.cookies.get("oleqs_access")?.value === (await sha256(code))) return NextResponse.next();
  if (pathname.startsWith("/api/")) return NextResponse.json({ error: "未登入" }, { status: 401 });
  return NextResponse.redirect(new URL("/login.html", req.url));
}

export const config = { matcher: ["/((?!_next/|favicon).*)"] };
