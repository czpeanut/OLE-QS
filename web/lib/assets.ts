// 附圖在 Supabase Storage 的物件鍵與讀取（鍵的算法與 apps/api/storage.py 相同：路徑的 SHA-1）。
import { createHash } from "crypto";
import { BUCKET } from "./supabase";

export function storageKey(rel: string): string {
  const h = createHash("sha1").update(rel.replace(/\\/g, "/"), "utf8").digest("hex");
  return `${h.slice(0, 2)}/${h.slice(2, 26)}.png`;
}

function objectUrl(rel: string): string {
  return `${process.env.SUPABASE_URL}/storage/v1/object/authenticated/${BUCKET}/${storageKey(rel)}`;
}

function auth(): Record<string, string> {
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY ?? "";
  return { Authorization: `Bearer ${key}`, apikey: key };
}

export async function fetchAsset(rel: string): Promise<Response> {
  return fetch(objectUrl(rel), { headers: auth(), cache: "no-store" });
}

// 只讀 PNG 檔頭拿寬度：排版時把圖印回原卷上的實際大小
const widthCache = new Map<string, number>();
export async function pngWidths(files: string[]): Promise<Map<string, number>> {
  const out = new Map<string, number>();
  await Promise.all([...new Set(files)].map(async (f) => {
    if (widthCache.has(f)) { out.set(f, widthCache.get(f)!); return; }
    try {
      const r = await fetch(objectUrl(f), { headers: { ...auth(), Range: "bytes=0-23" }, cache: "no-store" });
      const b = Buffer.from(await r.arrayBuffer());
      if (b.length >= 24 && b.readUInt32BE(0) === 0x89504e47) {
        const w = b.readUInt32BE(16);
        widthCache.set(f, w);
        out.set(f, w);
      }
    } catch { /* 拿不到寬度就用預設大小 */ }
  }));
  return out;
}
