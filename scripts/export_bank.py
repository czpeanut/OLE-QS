#!/usr/bin/env python3
"""把線上題庫（Supabase）匯出成給其他專案取用的快照：data/export/。

    python scripts/export_bank.py            # 需要 .env.local：SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY

產出（格式見 docs/USING_THE_BANK.md）：
- questions.jsonl.gz   每行一題，題幹、選項、答案、單元、題型、難度、附圖檔名都在同一筆
- papers.jsonl.gz      每行一份原卷（學校、段考、大題）
- taxonomy/            課程單元表、數學題型目錄（從 web/lib/data 複製）
- manifest.json        匯出時間、筆數、欄位版本
"""

from __future__ import annotations

import gzip
import json
import shutil
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batch_api import _env  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "export"
SCHEMA_VERSION = 1
FORMATS = {0: "choice", 1: "fill", 2: "apply", 3: "other", 9: "drill"}   # sort_key // 10,000,000
SPLIT = {"自然", "社會"}                                              # 這兩科的題目另有子科（理化、歷史…）


def pull(table: str, select: str, key: str = "id", page: int = 1000) -> list[dict]:
    """依主鍵分頁（keyset）讀完整張表。"""
    url, k = _env("SUPABASE_URL").rstrip("/"), _env("SUPABASE_SERVICE_ROLE_KEY")
    H = {"apikey": k, "Authorization": f"Bearer {k}"}
    s = requests.Session()
    rows, last = [], None
    while True:
        params = {"select": select, "order": f"{key}.asc", "limit": str(page)}
        if last is not None:
            params[key] = f"gt.{last}"
        for attempt in range(5):
            r = s.get(f"{url}/rest/v1/{table}", params=params, headers=H, timeout=120)
            if r.ok:
                break
            time.sleep(3 * (attempt + 1))
        r.raise_for_status()
        batch = r.json()
        rows += batch
        if len(batch) < page:
            return rows
        last = batch[-1][key]


def main() -> int:
    t0 = time.time()
    specs = {
        "document": ("id,title,school,exam_name,academic_year_roc,semester,exam_seq,grade,subject,sub_subject,"
                     "scope_note,publisher,page_count,total_score", "id"),
        "section": ("id,document_id,ord,name,type,per_item_score,declared_count", "id"),
        "question": ("id,document_id,section_ord,number,type,stem_md,group_stem,answer,answer_status,answer_source,"
                     "explanation_md,difficulty,score,page,parent_id,shared_asset_key,unit_publisher,unit_subject,"
                     "unit_code,unit_title,unit_chapter,status,review_note,sort_key", "id"),
        "option": ("id,question_id,label,content_md,asset_file,ord", "id"),
        "asset": ("id,document_id,question_id,key,label,kind,scope,file,markdown,alt", "id"),
        "tag": ("id,question_id,axis,value", "id"),
        "question_source": ("id,question_id,city,school_short", "id"),
    }
    with ThreadPoolExecutor(len(specs)) as pool:
        futs = {t: pool.submit(pull, t, sel, key) for t, (sel, key) in specs.items()}
        data = {t: f.result() for t, f in futs.items()}
    print("讀取完成：" + "、".join(f"{t} {len(v):,}" for t, v in data.items()), flush=True)

    docs = {d["id"]: d for d in data["document"]}
    sections = defaultdict(list)
    for s in data["section"]:
        sections[s["document_id"]].append(s)
    opts = defaultdict(list)
    for o in data["option"]:
        opts[o["question_id"]].append(o)
    q_assets, shared = defaultdict(list), {}
    for a in data["asset"]:
        if a["question_id"]:
            q_assets[a["question_id"]].append(a)
        else:
            shared[(a["document_id"], a["key"])] = a
    tags = defaultdict(lambda: defaultdict(list))
    for t in data["tag"]:
        tags[t["question_id"]][t["axis"]].append(t["value"])
    src = {s["question_id"]: s for s in data["question_source"]}
    patterns = json.loads((REPO / "web/lib/data/patterns.json").read_text(encoding="utf-8"))

    def pattern(qid: str):
        for v in tags[qid].get("pattern", []):
            unit, code = v.split(":")
            t = next((x for x in patterns.get(unit, {}).get("types", []) if x["code"] == code), None)
            if t:
                return {"key": v, "name": t["name"]}
        return None

    def image(a: dict) -> dict:
        out = {"key": a["key"], "label": a["label"], "file": a["file"]}
        if a.get("markdown"):
            out["markdown"] = a["markdown"]
        return out

    OUT.mkdir(parents=True, exist_ok=True)
    n_q = 0
    sec_name = {(s["document_id"], s["ord"]): s["name"] for s in data["section"]}
    with gzip.open(OUT / "questions.jsonl.gz", "wt", encoding="utf-8", compresslevel=9) as f:
        for q in sorted(data["question"], key=lambda q: (q["document_id"], q["section_ord"] or 0, q["number"] or 0)):
            d = docs[q["document_id"]]
            note = q.get("review_note") or ""
            sk = q.get("sort_key")
            sa = shared.get((q["document_id"], q["shared_asset_key"])) if q.get("shared_asset_key") else None
            rec = {
                "id": q["id"], "paper_id": q["document_id"],
                "subject": d["subject"],
                "sub_subject": q["unit_subject"] if d["subject"] in SPLIT and q["unit_subject"] in ("理化", "生物", "地科", "歷史", "地理", "公民") else (d.get("sub_subject") if d["subject"] in SPLIT else None),
                "grade": d["grade"], "semester": d["semester"], "exam_seq": d["exam_seq"], "year": d["academic_year_roc"],
                "school": d["school"], "city": (src.get(q["id"]) or {}).get("city"),
                "school_short": (src.get(q["id"]) or {}).get("school_short"),
                "section": q["section_ord"], "section_name": sec_name.get((q["document_id"], q["section_ord"])),
                "number": q["number"], "type": q["type"],
                "format": FORMATS.get(sk // 10_000_000) if sk is not None else None,
                "stem": q["stem_md"] or "", "group_stem": q["group_stem"], "group_id": q["parent_id"],
                "options": [{"label": o["label"], "text": o["content_md"] or "", "image": o["asset_file"]}
                            for o in sorted(opts[q["id"]], key=lambda o: o["ord"] or 0)],
                "answer": q["answer"], "answer_status": q["answer_status"], "answer_source": q["answer_source"],
                "explanation": q["explanation_md"], "score": q["score"], "difficulty": q["difficulty"],
                "unit": ({"publisher": q["unit_publisher"], "subject": q["unit_subject"], "code": q["unit_code"],
                          "title": q["unit_title"], "chapter": q["unit_chapter"]} if q["unit_code"] else None),
                "pattern": pattern(q["id"]),
                "textbook_tags": tags[q["id"]].get("textbook", []),
                "images": [image(a) for a in sorted(q_assets[q["id"]], key=lambda a: a["key"]) if a["file"]],
                "shared_images": [image(sa)] if sa and sa.get("file") else [],
                "shared_text": sa["markdown"] if sa and not sa.get("file") and sa.get("markdown") else None,
                "sort_key": sk, "status": q["status"],
                "corrected": ("原卷有誤" if note.startswith("原卷有誤已修正") else
                              "擷取錯誤" if note.startswith("擷取錯誤已修正") else None),
                "page": q["page"],
            }
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            n_q += 1
    with gzip.open(OUT / "papers.jsonl.gz", "wt", encoding="utf-8", compresslevel=9) as f:
        for d in sorted(docs.values(), key=lambda d: d["id"]):
            d = {**d, "sections": [{k: s[k] for k in ("ord", "name", "type", "per_item_score", "declared_count")}
                                   for s in sorted(sections[d["id"]], key=lambda s: s["ord"])]}
            f.write(json.dumps(d, ensure_ascii=False, separators=(",", ":")) + "\n")
    tax = OUT / "taxonomy"
    tax.mkdir(exist_ok=True)
    shutil.copy(REPO / "web/lib/data/curriculum.json", tax / "curriculum.json")
    shutil.copy(REPO / "web/lib/data/patterns.json", tax / "math_patterns.json")
    manifest = {"schema_version": SCHEMA_VERSION, "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "source": "Supabase（線上題庫）", "questions": n_q, "papers": len(docs),
                "questions_usable": sum(1 for q in data["question"] if q["status"] != "rejected"),
                "images": len(data["asset"]) + sum(1 for o in data["option"] if o["asset_file"])}
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file()) / 2**20
    print(f"匯出 {n_q:,} 題、{len(docs):,} 份卷 → {OUT.relative_to(REPO)}（{size:.1f} MB，{time.time() - t0:.0f} 秒）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
