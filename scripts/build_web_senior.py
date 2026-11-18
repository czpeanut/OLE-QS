#!/usr/bin/env python3
"""把高中題庫的網站靜態資料併進 web/lib/data（國中的資料不動）。

    python scripts/build_web_senior.py          # 先跑 scripts/senior_upload.py build

- senior.json：科目 → 子科 → 單元（代號、名稱、冊、章）
- counts.json：先移除舊的高中列（年級 ≥ 10），再加上高中各單元題數（版本欄是「標準」）、科目題數、學年度
- patterns.json：加上高中題型目錄，鍵是單元代號（例如 MA-1-1-1），國中的鍵（7-1_1-1）不動
"""
import json, sqlite3
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "web/lib/data"
DB = REPO / "out/senior_upload.db"
SUBJ = {"數學": "數學", "國文": "國文", "物理": "自然", "化學": "自然", "生物": "自然", "地科": "自然",
        "歷史": "社會", "地理": "社會", "公民與社會": "社會"}


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = yaml.safe_load((REPO / "data/curriculum/senior.yaml").read_text(encoding="utf-8"))["subjects"]
    senior: dict = {}
    for sub, books in cur.items():
        for b in books:
            for u in b["units"]:
                senior.setdefault(SUBJ[sub], {}).setdefault(sub, []).append(
                    {"code": str(u["code"]), "title": u["title"], "book": b["book"], "chapter": u.get("chapter_title")})
    (OUT / "senior.json").write_text(json.dumps(senior, ensure_ascii=False))

    counts = json.loads((OUT / "counts.json").read_text(encoding="utf-8"))
    junior_units = [r for r in counts["units"] if r[1] < 10]
    old_senior = [r for r in counts["units"] if r[1] >= 10]
    rows = con.execute("""
        select d.subject, d.grade, d.semester, q.unit_subject, q.unit_code, coalesce(q.unit_publisher, ''), count(*)
        from question q join document d on d.id = q.document_id
        where q.status != 'rejected' and q.unit_code is not null group by 1, 2, 3, 4, 5, 6""").fetchall()
    counts["units"] = junior_units + [list(r) for r in rows]
    prev = counts.get("senior_subjects", {})
    subj = dict(con.execute("""select d.subject, count(*) from question q join document d on d.id = q.document_id
                               where q.status != 'rejected' group by 1""").fetchall())
    for k in set(prev) | set(subj):
        counts["subjects"][k] = counts["subjects"].get(k, 0) - prev.get(k, 0) + subj.get(k, 0)
    counts["senior_subjects"] = subj
    years = {r[0] for r in con.execute("select distinct academic_year_roc from document")}
    counts["years"] = sorted(set(counts["years"]) | years, reverse=True)
    (OUT / "counts.json").write_text(json.dumps(counts, ensure_ascii=False))

    n_pat = dict(con.execute("select value, count(*) from tag where axis = 'pattern' group by 1").fetchall())
    pats = json.loads((OUT / "patterns.json").read_text(encoding="utf-8"))
    pats = {k: v for k, v in pats.items() if "_" in k}            # 國中鍵是「7-1_1-1」，高中鍵沒有底線
    for f in sorted((REPO / "data/curriculum/senior_patterns").glob("*/*.yaml")):
        c = yaml.safe_load(f.read_text(encoding="utf-8"))
        code = str(c["unit"]["code"])
        types = [{"code": t["code"], "name": t["name"], "desc": t.get("desc", ""), "n": n_pat.get(f"{code}:{t['code']}", 0)}
                 for t in c.get("types") or []]
        pats[code] = {"title": c["unit"]["title"], "types": [t for t in types if t["n"]]}
    (OUT / "patterns.json").write_text(json.dumps(pats, ensure_ascii=False))
    print(f"高中單元題數 {len(rows):,} 筆（取代舊的 {len(old_senior)} 筆）；高中題數 {sum(subj.values()):,}；"
          f"題型目錄 {sum(1 for k in pats if '_' not in k)} 個單元")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
