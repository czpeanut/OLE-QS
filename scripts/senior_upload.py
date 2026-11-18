#!/usr/bin/env python3
"""高中題庫上站：data/high_school/bank → Supabase。

  python scripts/senior_upload.py build             # 挑出暫緩題、轉成資料列（暫存 SQLite：out/senior_upload.db）
  python scripts/senior_upload.py upload            # 寫入 Supabase（只動高中卷：先刪這些卷的舊資料再寫）
  python scripts/senior_upload.py figures --assets <合庫的 assets 資料夾>   # 上傳用到的附圖

暫緩題（不上站，等確認）寫在 data/high_school/upload_holdback.csv：
  - 執行 agent 分章節時留了備註的題（缺字、缺圖、答案可疑、排版錯位…）
  - 判斷不了單元的題
  - 同圖多掛、待確認的題
  - 以上任一題所在的整個題組（題組要整組出）
注意：不能用 load_supabase.py 搬高中卷 —— 它連自增 id 一起寫，會覆蓋國中的選項／附圖／標籤。
這裡除了 document、question 以外都不帶 id，由資料庫配號。
"""
from __future__ import annotations

import argparse, csv, json, os, re, sqlite3, sys, time
from pathlib import Path

import requests
import yaml

REPO = Path(__file__).resolve().parent.parent
BANK = REPO / "data/high_school/bank"
NOTES = REPO / "data/high_school/label_notes.csv"
HOLD = REPO / "data/high_school/upload_holdback.csv"
FIXES = REPO / "data/high_school/fix_review.csv"
DB = REPO / "out/senior_upload.db"
# 同圖多掛待人工確認（景美化學 31～33 已確認是原卷共用圖，不暫緩）
SHARED_FIG_PENDING = ["doc_112_高雄_三民_自然_生物_選修生物I_g11s1e2_s3_40", "doc_112_高雄_三民_自然_生物_選修生物I_g11s1e2_s3_41",
                      "doc_112_金門_金門高中_自然_生物_選修生物II_g11s1e2_s3_26", "doc_112_金門_金門高中_自然_生物_選修生物II_g11s1e2_s3_36"]

sys.path.insert(0, str(REPO / "scripts"))


def env() -> tuple[str, dict]:
    from batch_api import _env
    url, key = _env("SUPABASE_URL").rstrip("/"), _env("SUPABASE_SERVICE_ROLE_KEY")
    return url, {"apikey": key, "Authorization": f"Bearer {key}"}


def holdback() -> dict[str, str]:
    hold: dict[str, str] = {}
    fixed = set()
    if FIXES.is_file():                       # 執行 agent 手動修正、審核收下的題（scripts/senior_apply_fixes.py）
        fixed = {r["題目id"] for r in csv.DictReader(FIXES.open(encoding="utf-8")) if r["審核"] == "收"}
    for r in csv.DictReader(NOTES.open(encoding="utf-8")):
        if r["題目id"] not in fixed:
            hold[r["題目id"]] = "分章節備註：" + r["備註"]
    for q in SHARED_FIG_PENDING:
        hold.setdefault(q, "同圖多掛待確認")
    groups: dict[tuple[str, str], list[str]] = {}
    for y in sorted(BANK.glob("*.yaml")):
        d = yaml.safe_load(y.read_text())
        for q in d["questions"]:
            if not (q.get("tags") or {}).get("unit"):
                hold.setdefault(q["id"], "沒有單元")
            if q.get("group_stem"):
                groups.setdefault((d["document"]["id"], q["group_stem"]), []).append(q["id"])
    for (_, _), ids in groups.items():
        bad = [i for i in ids if i in hold]
        if bad:
            for i in ids:
                hold.setdefault(i, f"同題組的 {bad[0].rsplit('_', 2)[-2]}_{bad[0].rsplit('_', 1)[-1]} 暫緩")
    return hold


def build() -> int:
    hold = holdback()
    with HOLD.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["題目id", "暫緩原因"])
        for k in sorted(hold):
            w.writerow([k, hold[k]])
    DB.parent.mkdir(exist_ok=True)
    DB.unlink(missing_ok=True)
    os.environ["OLEQS_DB"] = str(DB)
    os.environ.pop("OLEQS_DATABASE_URL", None)
    sys.path.insert(0, str(REPO))
    from apps.api.db import init_db, get_session
    from apps.api.importer import import_document
    init_db()
    kept = total = 0
    patterns: list[tuple[str, str]] = []
    with get_session() as s:
        for y in sorted(BANK.glob("*.yaml")):
            d = yaml.safe_load(y.read_text())
            doc = d["document"]
            subj = doc.get("sub_subject") or doc["subject"]
            qs = []
            for q in d["questions"]:
                total += 1
                if q["id"] in hold:
                    continue
                t = q.get("tags") or {}
                u = t["unit"]
                q = dict(q)
                q["tags"] = {"chapter": {"publisher": "標準", "subject": subj, "code": u["code"], "title": u["title"],
                                         "chapter": f"{u['book']}｜{u.get('chapter') or ''}".rstrip("｜")},
                             "textbook": t.get("textbook"), "labeled_by": "agent"}
                if t.get("pattern"):
                    patterns.append((q["id"], t["pattern"]["key"]))
                qs.append(q)
            if not qs:
                continue
            d = dict(d, questions=qs)
            st = import_document(s, d, doc.get("source_file"))
            kept += st["kept"]
            s.commit()
    con = sqlite3.connect(DB)
    con.executemany("insert into tag (question_id, axis, value, is_primary, labeled_by) "
                    "select ?, 'pattern', ?, 1, 'agent' where exists (select 1 from question where id = ? and status != 'rejected')",
                    [(q, p, q) for q, p in patterns])
    con.commit()
    n = {t: con.execute(f"select count(*) from {t}").fetchone()[0] for t in
         ("document", "question", "option", "asset", "tag", "question_source", "question_fts")}
    rej = con.execute("select count(*) from question where status = 'rejected'").fetchone()[0]
    print(f"高中 {total:,} 題：暫緩 {len(hold):,}（{HOLD.name}）、送進資料庫 {n['question']:,}（品管剔除 {rej:,}）")
    print("資料列：" + "、".join(f"{k} {v:,}" for k, v in n.items()))
    return 0


def upload(workers: int = 4) -> int:
    url, H = env()
    url += "/rest/v1"
    s = requests.Session()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    docs = [r["id"] for r in con.execute("select id from document")]
    if any(int(r["grade"]) < 10 for r in con.execute("select grade from document")):
        sys.exit("暫存資料庫裡有國中卷，停止")

    def call(method: str, table: str, params=None, body=None, prefer="return=minimal"):
        last = ""
        for attempt in range(6):
            try:
                r = s.request(method, f"{url}/{table}", params=params, timeout=300,
                              headers={**H, "Content-Type": "application/json", "Prefer": prefer},
                              data=json.dumps(body, ensure_ascii=False, default=str).encode() if body is not None else None)
                if r.status_code in (200, 201, 204):
                    return
                if r.status_code < 500 and r.status_code != 429:
                    raise RuntimeError(f"{method} {table}: {r.status_code} {r.text[:300]}")
                last = f"{r.status_code} {r.text[:200]}"
            except requests.RequestException as e:
                last = str(e)[:200]
            time.sleep(2 ** attempt)
        raise RuntimeError(f"{method} {table} 重試失敗：{last}")

    # 1. 清掉這些高中卷在線上的舊資料（題目刪除時選項、附圖、出處、標籤、搜尋文字級聯刪除）
    # 一卷一卷刪：一次刪多卷會超過資料庫的執行時間上限（級聯刪除選項、標籤、搜尋文字）
    from concurrent.futures import ThreadPoolExecutor

    def wipe(doc_id: str) -> None:
        # 級聯刪除整卷會逾時：先分批刪子表，再刪題目
        for attempt in range(6):                  # 偶爾代理伺服器斷線，重試
            try:
                r = s.get(f"{url}/question", params={"select": "id", "document_id": f"eq.{doc_id}"}, headers=H, timeout=120)
                r.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 5:
                    raise
                time.sleep(2 ** attempt)
        ids = [x["id"] for x in r.json()]
        for i in range(0, len(ids), 25):
            chunk = "in.(" + ",".join(f'"{x}"' for x in ids[i:i + 25]) + ")"
            for t in ("option", "tag", "question_source", "question_search", "asset", "paper_item"):
                call("DELETE", t, {"question_id": chunk})
            call("DELETE", "question", {"id": chunk})
        for t in ("asset", "section"):
            call("DELETE", t, {"document_id": f"eq.{doc_id}"})
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(wipe, docs))
    print(f"已清除 {len(docs)} 卷的舊資料", flush=True)

    def rows(sql: str, drop_id: bool, json_cols=(), bool_cols=()):
        for r in con.execute(sql):
            d = dict(r)
            if drop_id:
                d.pop("id", None)
            for c in json_cols:
                if isinstance(d.get(c), str):
                    d[c] = json.loads(d[c])
            for c in bool_cols:
                if d.get(c) is not None:
                    d[c] = bool(d[c])
            yield {k: (v.replace("\x00", "") if isinstance(v, str) else v) for k, v in d.items()}

    plan = [
        ("document", "select * from document", False, (), (), "resolution=merge-duplicates,return=minimal"),
        ("section", "select * from section", True, (), (), "return=minimal"),
        ("question", "select * from question", False, ("answer", "uncertain_spans"), (), "resolution=merge-duplicates,return=minimal"),
        ("option", "select * from option", True, (), (), "return=minimal"),
        ("asset", "select * from asset", True, ("bbox", "used_by"), ("pending",), "return=minimal"),
        ("question_source", "select * from question_source", True, (), (), "return=minimal"),
        ("tag", "select * from tag", True, (), ("is_primary",), "return=minimal"),
        ("question_search", "select question_id, body from question_fts", False, (), (), "resolution=merge-duplicates,return=minimal"),
    ]
    for table, sql, drop, jc, bc, prefer in plan:
        t0, n, batch = time.time(), 0, []
        for r in rows(sql, drop, jc, bc):
            batch.append(r)
            n += 1
            if len(batch) >= 500:
                call("POST", table, body=batch, prefer=prefer)
                batch = []
        if batch:
            call("POST", table, body=batch, prefer=prefer)
        print(f"{table}: {n:,} 列，{time.time() - t0:.0f} 秒", flush=True)
    return 0


def figures(assets: Path, workers: int = 16) -> int:
    from concurrent.futures import ThreadPoolExecutor
    sys.path.insert(0, str(REPO))
    from apps.api.storage import BUCKET, storage_key
    url, H = env()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    files = sorted({r[0] for r in con.execute("select file from asset where file is not null union "
                                              "select asset_file from option where asset_file is not null")})
    missing = [f for f in files if not (assets / f).is_file()]
    if missing:
        sys.exit(f"缺 {len(missing)} 張附圖，例如 {missing[:3]}")
    s = requests.Session()

    def put(rel: str):
        for attempt in range(5):
            try:
                r = s.post(f"{url}/storage/v1/object/{BUCKET}/{storage_key(rel)}", data=(assets / rel).read_bytes(), timeout=120,
                           headers={**H, "Content-Type": "image/png", "x-upsert": "true"})
                if r.status_code in (200, 201):
                    return None
                err = f"{r.status_code} {r.text[:100]}"
            except requests.RequestException as e:
                err = str(e)[:100]
            time.sleep(2 ** attempt)
        return f"{rel}：{err}"

    with ThreadPoolExecutor(workers) as pool:
        errs = [e for e in pool.map(put, files) if e]
    print(f"附圖 {len(files):,} 張，失敗 {len(errs)}")
    for e in errs[:20]:
        print(" ", e)
    return 1 if errs else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("build")
    sp.add_parser("upload")
    f = sp.add_parser("figures"); f.add_argument("--assets", type=Path, required=True)
    a = ap.parse_args()
    if a.cmd == "build":
        return build()
    if a.cmd == "upload":
        return upload()
    return figures(a.assets)


if __name__ == "__main__":
    sys.exit(main())
