import { NextRequest, NextResponse } from "next/server";
import { createHash } from "crypto";

// 暫時的通行碼登入（之後換成 Supabase 的 Google 登入）：比對成功後寫入 cookie
export async function POST(req: NextRequest) {
  const form = await req.formData();
  const code = String(form.get("code") ?? "");
  const want = process.env.SITE_ACCESS_CODE ?? "";
  const res = NextResponse.redirect(new URL(code && code === want ? "/" : "/login.html?err=1", req.url), 303);
  if (code && code === want) {
    res.cookies.set("oleqs_access", createHash("sha256").update(want).digest("hex"),
                    { httpOnly: true, secure: true, sameSite: "lax", maxAge: 60 * 60 * 24 * 30, path: "/" });
  }
  return res;
}
