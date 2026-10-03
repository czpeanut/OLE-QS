import { createClient, SupabaseClient } from "@supabase/supabase-js";

// 只在伺服器端（API 路由）使用 service role：資料表全部開了 RLS 且沒有 policy，
// 瀏覽器拿 anon 金鑰讀不到任何東西，所有存取都經過這裡。
let client: SupabaseClient | null = null;

export function db(): SupabaseClient {
  if (!client) {
    const url = process.env.SUPABASE_URL, key = process.env.SUPABASE_SERVICE_ROLE_KEY;
    if (!url || !key) throw new Error("缺少 SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY");
    client = createClient(url, key, { auth: { persistSession: false } });
  }
  return client;
}

export const BUCKET = "assets";
