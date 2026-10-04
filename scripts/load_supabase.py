#!/usr/bin/env python3
"""把本機題庫資料庫（data/oleqs.db，由 apps.api.importer 建立）整批寫進 Supabase。

這台主機連不到 Postgres 的連線埠，所以走 Supabase 的 REST API（PostgREST），
以 service role 金鑰分批 upsert；重跑會覆蓋同 id 的列，不會重複。

    python scripts/load_supabase.py [--db data/oleqs.db] [--only <doc 片段>] [--tables question,option]

需要 .env.local：SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY；資料表先用 deploy/supabase/schema.sql 建好。
- 只寫入「文件 + 題目 + 選項 + 附圖 + 出處 + 標籤 + 搜尋文字」，組好的試卷（paper）不搬
- --only 只搬檔名含該片段的卷（測試用）；不帶時整批搬，最後重設自增序號
- --replace-docs：先刪掉這些卷在 Supabase 上的舊題目再寫（重新擷取過的卷用）
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from batch_api import _env  # noqa: E402

# 依外鍵順序；第二欄是 JSON 欄位，第三欄是布林欄位（SQLite 存 0/1）
TABLES = [
    ("document", (), ()),
    ("section", (), ()),
    ("question", ("answer", "uncertain_spans"), ()),
    ("option", (), ()),
    ("asset", ("bbox", "used_by"), ("pending",)),
    ("question_source", (), ()),
    ("tag", (), ("is_primary",)),
]
BATCH = 500


def clean(v):
    """Postgres 的 text 不收 NUL 字元（擷取時偶爾夾帶），遞迴拿掉。"""
    if isinstance(v, str):
        return v.replace("\x00", "")
    if isinstance(v, list):
        return [clean(x) for x in v]
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    return v


def rows(con: sqlite3.Connection, table: str, doc_filter: str | None):
    where, args = "", ()
    if doc_filter:
        col = {"document": "id", "section": "document_id", "question": "document_id", "asset": "document_id"}.get(table)
        if col:
            where, args = f" where {col} like ?", (f"%{doc_filter}%",)
        else:          # option / question_source / tag 依所屬題目篩選
            where, args = " where question_id in (select id from question where document_id like ?)", (f"%{doc_filter}%",)
    cur = con.execute(f'select * from "{table}"{where}', args)
    cols = [c[0] for c in cur.description]
    for r in cur:
        yield dict(zip(cols, r))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=REPO / "data" / "oleqs.db")
    ap.add_argument("--only")
    ap.add_argument("--tables", help="只搬這些表（逗號分隔），預設全部")
    ap.add_argument("--replace-docs", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    url = _env("SUPABASE_URL").rstrip("/") + "/rest/v1"
    key = _env("SUPABASE_SERVICE_ROLE_KEY")
    H = {"Authorization": f"Bearer {key}", "apikey": key, "Content-Type": "application/json",
         "Prefer": "resolution=merge-duplicates,return=minimal"}
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    session = requests.Session()

    if args.replace_docs and args.only:
        # 題目刪掉時選項、附圖、出處、標籤、搜尋文字會一起級聯刪除
        r = session.delete(f"{url}/question", headers=H, params={"document_id": f"like.*{args.only}*"}, timeout=120)
        r.raise_for_status()

    def post(table: str, batch: list[dict]) -> None:
        body = json.dumps(batch, ensure_ascii=False, default=str).encode("utf-8")
        for attempt in range(6):
            try:
                r = session.post(f"{url}/{table}", headers=H, data=body, timeout=300)
                if r.status_code in (200, 201, 204):
                    return
                err = f"{r.status_code} {r.text[:300]}"
                if r.status_code < 500 and r.status_code != 429:
                    raise RuntimeError(f"{table}: {err}")
            except requests.RequestException as exc:
                err = str(exc)[:200]
            time.sleep(2 ** attempt)
        raise RuntimeError(f"{table}: {err}")

    want = set(args.tables.split(",")) if args.tables else None
    plan = [t for t in TABLES if not want or t[0] in want]
    if not want or "question_search" in want:
        plan.append(("question_search", (), ()))
    for table, json_cols, bool_cols in plan:
        t0, n = time.time(), 0
        if table == "question_search":
            src = ({"question_id": r["question_id"], "body": r["body"]} for r in rows(con, "question_fts", None)
                   if not args.only or args.only in r["question_id"])
        else:
            src = rows(con, table, args.only)
        with ThreadPoolExecutor(args.workers) as pool:
            futs, batch = [], []
            for r in src:
                for c in json_cols:
                    if isinstance(r.get(c), str):
                        r[c] = json.loads(r[c])
                for c in bool_cols:
                    if r.get(c) is not None:
                        r[c] = bool(r[c])
                batch.append({k: clean(v) for k, v in r.items()})
                n += 1
                if len(batch) >= BATCH:
                    futs.append(pool.submit(post, table, batch))
                    batch = []
                if len(futs) >= args.workers * 4:        # 別一次把整張表塞進記憶體
                    futs.pop(0).result()
            if batch:
                futs.append(pool.submit(post, table, batch))
            for f in futs:
                f.result()
        print(f"{table}: {n:,} 列，{time.time() - t0:.0f} 秒", flush=True)

    if not args.only:
        r = session.post(f"{url}/rpc/oleqs_reset_sequences", headers=H, json={}, timeout=60)
        print("重設自增序號：", r.status_code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
