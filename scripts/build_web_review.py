#!/usr/bin/env python3
"""由審題台原版（apps/review/index.html）產生 OLE 題庫網站上的審題台（web/public/review.html）。

原版跑在 claude.ai 的頁面裡，用平台內建的資料庫與使用者身分；網站版的介面與功能完全相同，
只換掉三個接點：
- 題目資料：從 /api/review/<站>/files/… 讀（Supabase Storage）
- 回報、審完、邊緣調整：存進 Supabase（/api/review/<站>/db/…），用輪詢同步其他人的修改
- 審題者：第一次開啟時輸入名字，記在瀏覽器；名字存在共用的 people 集合供其他人顯示

    python scripts/build_web_review.py
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SHIM = r"""<script>
/* ── 網站版接點：取代 claude.ai 頁面的資料庫與使用者身分 ── */
(() => {
  const SITE = location.pathname.split("/").filter(Boolean)[1] || "7-1";
  window.REVIEW_BASE = `/api/review/${SITE}/files/`;
  window.REVIEW_SITES = Object.fromEntries(["7-1","7-2","8-1","8-2","9-1","9-2"].map(k => [k, `/review/${k}`]));
  const api = (site, col, id) => `/api/review/${site}/db/${col}` + (id ? "/" + encodeURIComponent(id) : "");
  const listeners = new Map();          // 集合 → 訂閱函式
  async function refresh(col){
    const subs = listeners.get(col); if (!subs) return;
    try {
      const r = await fetch(api(SITE, col)); if (!r.ok) throw new Error(r.status);
      const rows = await r.json();
      const snap = {docs: rows.map(x => ({id: x.id, data: () => x.data}))};
      subs.forEach(s => s.cb(snap));
    } catch (e) { subs.forEach(s => s.err && s.err(e)); }
  }
  async function write(method, col, id, body){
    const r = await fetch(api(SITE, col, id), {method, headers: {"Content-Type": "application/json"},
                                               body: body === undefined ? undefined : JSON.stringify(body)});
    if (!r.ok) { const e = new Error("儲存失敗"); e.code = r.status; throw e; }
    refresh(col);
  }
  const DB = {collection: col => ({
    doc: id => ({set: data => write("PUT", col, id, data), delete: () => write("DELETE", col, id)}),
    onSnapshot(cb, err){
      if (!listeners.has(col)) listeners.set(col, []);
      listeners.get(col).push({cb, err});
      refresh(col);
      return () => {};
    },
  })};
  // 每 20 秒同步一次其他審題者的修改（分頁不在前景時暫停）
  setInterval(() => { if (!document.hidden) listeners.forEach((_, col) => refresh(col)); }, 20000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) listeners.forEach((_, col) => refresh(col)); });

  function me(){
    let id = null, name = "";
    try { id = localStorage.getItem("oleqs.reviewer.id"); name = localStorage.getItem("oleqs.reviewer.name") || ""; } catch {}
    if (!id) { id = "r_" + Math.random().toString(36).slice(2, 12); try { localStorage.setItem("oleqs.reviewer.id", id); } catch {} }
    while (!name) {
      name = (prompt("請輸入你的名字（回報與審完紀錄會標示審題者）") || "").trim().slice(0, 30);
    }
    try { localStorage.setItem("oleqs.reviewer.name", name); } catch {}
    fetch(api("_all", "people", id), {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name})});
    return {id, name};
  }
  let names = null;
  const USER = {
    me: async () => me(),
    can: async () => true,
    async profiles(ids){
      if (!names) { try { names = Object.fromEntries((await (await fetch(api("_all", "people"))).json()).map(x => [x.id, x.data.name])); } catch { names = {}; } }
      return Object.fromEntries(ids.map(i => [i, {id: i, name: names[i] || ""}]));
    },
  };
  window.claude = {use: async name => name === "db" ? DB : name === "user" ? USER : null};
})();
</script>
"""


def main() -> int:
    src = (REPO / "apps/review/index.html").read_text(encoding="utf-8")
    patches = [
        ('fetch("data/index.json")', 'fetch(REVIEW_BASE + "data/index.json")'),
        ('fetch("data/" + INDEX.groups[gi].f)', 'fetch(REVIEW_BASE + "data/" + INDEX.groups[gi].f)'),
        ("INDEX = await r.json();", "INDEX = await r.json(); INDEX.sites = REVIEW_SITES;"),
        ('target="_top"', 'target="_self"'),
        ("這個檢視無法存取回報資料庫：回報會改成複製文字，請貼給 Claude。", "無法連上審題紀錄資料庫，請重新整理頁面。"),
    ]
    for a, b in patches:
        if a not in src:
            raise SystemExit(f"原版審題台找不到要替換的片段：{a}")
        src = src.replace(a, b)
    i = src.index("<script>\n\"use strict\";")
    out = src[:i] + SHIM + src[i:]
    dest = REPO / "web/public/review.html"
    dest.write_text(out, encoding="utf-8")
    print(f"→ {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
