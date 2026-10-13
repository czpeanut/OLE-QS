#!/usr/bin/env python3
"""修正 PDF 字型對應錯誤造成的錯字（原卷畫面上是對的，文字層卻是另一個字，擷取照抄了文字層）。

台灣常見的考卷 PDF 用到某些字型時，「平」的文字層是「帄」、「須」是「頇」等。這是擷取錯誤，不是原卷錯誤，
所以直接改正，不加「原卷有誤」的註記。改題幹、題組說明、選項；同時改題庫 YAML、data/oleqs.db 與 Supabase。

    python scripts/fix_glyphs.py            # 列出會改幾處
    python scripts/fix_glyphs.py --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batch_api import _env  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BANK = REPO / "data" / "bank"
DB = REPO / "data" / "oleqs.db"
# 只收在題庫裡出現、且逐一看過上下文都該換的字（「冺」有時是「利」有時是「敏」，不收）
GLYPHS = {"帄": "平", "頇": "須", "尌": "就", "貣": "起", "亯": "信", "賥": "質"}
TABLE = str.maketrans(GLYPHS)


def fix(s: str | None) -> str | None:
    return s.translate(TABLE) if s else s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    n_yaml = 0
    for p in sorted(BANK.glob("*.yaml")):
        raw = p.read_text(encoding="utf-8")
        if not any(ch in raw for ch in GLYPHS):
            continue
        d = yaml.safe_load(raw)
        changed = False
        for q in d.get("questions") or []:
            for k in ("stem", "group_stem"):
                if q.get(k) and fix(q[k]) != q[k]:
                    q[k], changed = fix(q[k]), True
            for o in q.get("options") or []:
                if o.get("content") and fix(o["content"]) != o["content"]:
                    o["content"], changed = fix(o["content"]), True
        if changed:
            n_yaml += 1
            if args.apply:
                p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")

    con = sqlite3.connect(DB)
    like = " or ".join(["{col} like ?"] * len(GLYPHS))
    pats = [f"%{ch}%" for ch in GLYPHS]
    qrows = con.execute(f"select id, stem_md, group_stem from question where "
                        f"{like.format(col='stem_md')} or {like.format(col='group_stem')}", pats + pats).fetchall()
    orows = con.execute(f"select id, content_md from option where {like.format(col='content_md')}", pats).fetchall()
    print(f"題庫檔 {n_yaml} 份；題目 {len(qrows)} 題、選項 {len(orows)} 個要改")
    if not args.apply:
        return 0
    url, key = _env("SUPABASE_URL").rstrip("/"), _env("SUPABASE_SERVICE_ROLE_KEY")
    H = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Prefer": "return=minimal"}
    s = requests.Session()
    for qid, stem, gs in qrows:
        con.execute("update question set stem_md=?, group_stem=? where id=?", (fix(stem), fix(gs), qid))
        s.patch(f"{url}/rest/v1/question", params={"id": f"eq.{qid}"}, headers=H,
                json={"stem_md": fix(stem), "group_stem": fix(gs)}, timeout=60).raise_for_status()
    for oid, content in orows:
        con.execute("update option set content_md=? where id=?", (fix(content), oid))
        s.patch(f"{url}/rest/v1/option", params={"id": f"eq.{oid}"}, headers=H,
                json={"content_md": fix(content)}, timeout=60).raise_for_status()
    con.commit()
    print("已寫回 YAML、oleqs.db、Supabase")
    return 0


if __name__ == "__main__":
    sys.exit(main())
