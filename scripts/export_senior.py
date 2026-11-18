"""高中題庫快照：data/high_school/bank → data/export_senior/（欄位與 data/export/ 相同，client/oleqs_bank.py 可直接讀）。

  python scripts/export_senior.py
  python client/oleqs_bank.py stats            # 設 OLEQS_EXPORT=data/export_senior 讀高中

附圖不在 repo：檔名與國中相同格式（<卷 id>/<key>.png），檔案由 scripts/round3_merge.py 重建，之後上傳 Supabase。
"""
import gzip, json, time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BANK = ROOT / "data/high_school/bank"
OUT = ROOT / "data/export_senior"
FORMAT = {"single": "choice", "multiple": "choice", "fill": "fill", "calc": "apply"}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    nq = nu = nimg = nheld = 0
    hold_csv = ROOT / "data/high_school/upload_holdback.csv"          # 暫緩上站的題：status＝held
    held = {l.split(",")[0] for l in hold_csv.read_text(encoding="utf-8").splitlines()[1:]} if hold_csv.is_file() else set()
    with gzip.open(OUT / "questions.jsonl.gz", "wt", encoding="utf-8", compresslevel=9) as fq, \
            gzip.open(OUT / "papers.jsonl.gz", "wt", encoding="utf-8", compresslevel=9) as fp:
        for y in sorted(BANK.glob("*.yaml")):
            d = yaml.safe_load(y.read_text())
            doc = d["document"]
            secs = {s["ord"]: s["name"] for s in doc.get("sections") or []}
            fp.write(json.dumps({
                "id": doc["id"], "level": "senior", "title": doc.get("title"), "school": doc.get("school"),
                "exam_name": doc.get("exam_name"), "academic_year_roc": doc["academic_year_roc"],
                "semester": doc["semester"], "exam_seq": doc["exam_seq"], "grade": doc["grade"],
                "subject": doc["subject"], "sub_subject": doc.get("sub_subject"), "course": doc.get("course"),
                "scope_note": doc.get("scope_note"), "publisher": None, "page_count": None, "total_score": None,
                "sections": [{"ord": s["ord"], "name": s["name"], "type": None, "per_item_score": None,
                              "declared_count": s.get("count")} for s in doc.get("sections") or []],
            }, ensure_ascii=False) + "\n")
            shared = {s["key"]: s for s in d.get("shared_assets") or []}
            group_first: dict[str, str] = {}
            for q in d["questions"]:
                tags = q.get("tags") or {}
                u = tags.get("unit")
                gid = None
                if q.get("group_stem"):
                    gid = group_first.setdefault(q["group_stem"], q["id"])
                sa = shared.get(q.get("shared_asset"))
                imgs = [{"key": a["key"], "label": a.get("label"), "file": a["file"]} for a in q.get("assets") or []]
                rec = {
                    "id": q["id"], "paper_id": doc["id"], "level": "senior",
                    "subject": doc["subject"], "sub_subject": doc.get("sub_subject"), "course": doc.get("course"),
                    "grade": doc["grade"], "semester": doc["semester"], "exam_seq": doc["exam_seq"],
                    "year": doc["academic_year_roc"], "school": doc.get("school"), "city": doc.get("city"),
                    "school_short": doc.get("school_short"),
                    "section": q["section"], "section_name": secs.get(q["section"]), "number": q["number"],
                    "type": q["type"], "format": FORMAT.get(q["type"]),
                    "stem": q["stem"], "group_stem": q.get("group_stem"), "group_id": gid,
                    "options": [{"label": o["label"], "text": o.get("content") or "",
                                 "image": (o.get("asset") or {}).get("file")} for o in q.get("options") or []],
                    "answer": q.get("answer"), "answer_status": q.get("answer_status") or "missing",
                    "answer_source": q.get("answer_source"), "explanation": q.get("explanation"),
                    "score": None, "difficulty": q.get("difficulty"),
                    "unit": ({"publisher": "標準", "subject": doc.get("sub_subject") or doc["subject"],
                              "code": u["code"], "title": u["title"], "chapter": u.get("chapter"), "book": u.get("book")}
                             if u else None),
                    "pattern": tags.get("pattern"),
                    "textbook_tags": [tags["textbook"]] if tags.get("textbook") else [],
                    "images": imgs,
                    "shared_images": [{"key": sa["key"], "label": sa.get("label"), "file": sa["file"]}] if sa else [],
                    "shared_text": None, "sort_key": None, "status": "held" if q["id"] in held else "reviewed", "corrected": None,
                    "page": q.get("page"), "label_note": tags.get("label_note"),
                }
                fq.write(json.dumps(rec, ensure_ascii=False) + "\n")
                nq += 1
                nheld += q["id"] in held
                nu += bool(u)
                nimg += len(imgs) + len(rec["shared_images"]) + sum(bool(o["image"]) for o in rec["options"])
    tax = OUT / "taxonomy"
    tax.mkdir(exist_ok=True)
    (tax / "curriculum.json").write_text(json.dumps(
        {"senior": yaml.safe_load((ROOT / "data/curriculum/senior.yaml").read_text())["subjects"]},
        ensure_ascii=False), encoding="utf-8")
    pats = {}
    for f in sorted((ROOT / "data/curriculum/senior_patterns").glob("*/*.yaml")):
        c = yaml.safe_load(f.read_text())
        pats[str(c["unit"]["code"])] = {"unit": c["unit"], "types": c.get("types") or []}
    (tax / "patterns.json").write_text(json.dumps(pats, ensure_ascii=False), encoding="utf-8")
    papers = len(list(BANK.glob("*.yaml")))
    (OUT / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "level": "senior", "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": "data/high_school/bank（第三輪萃取＋人工校對＋逐題分章節）",
        "questions": nq, "papers": papers, "questions_usable": nq - nheld, "questions_held": nheld, "questions_with_unit": nu, "images": nimg,
    }, ensure_ascii=False, indent=1))
    print(f"{papers} 卷、{nq:,} 題（有單元 {nu:,}）、附圖 {nimg:,} → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
