#!/usr/bin/env python3
"""產生 web/（Vercel 版）用的靜態資料：章節表、英語主題、各單元題數、篩選選項。

    python scripts/build_web_data.py [--db data/oleqs.db]

題數在題庫重新匯入後要重跑一次（Supabase 的 REST API 不做 GROUP BY，題數改成建置時算好）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "web" / "lib" / "data"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=REPO / "data" / "oleqs.db")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    junior = yaml.safe_load((REPO / "data/curriculum/junior.yaml").read_text(encoding="utf-8"))["grades"]
    topics = yaml.safe_load((REPO / "data/curriculum/english_topics.yaml").read_text(encoding="utf-8"))["topics"]
    (OUT / "curriculum.json").write_text(json.dumps({"junior": junior, "english": topics}, ensure_ascii=False))

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    counts = con.execute("""
        select d.subject, d.grade, d.semester, q.unit_subject, q.unit_code, coalesce(q.unit_publisher, ''), count(*)
        from question q join document d on d.id = q.document_id
        where q.status != 'rejected' and q.unit_code is not null
        group by 1, 2, 3, 4, 5, 6""").fetchall()
    subjects = con.execute("""
        select d.subject, count(*) from question q join document d on d.id = q.document_id
        where q.status != 'rejected' group by 1""").fetchall()
    years = [r[0] for r in con.execute("select distinct academic_year_roc from document order by 1 desc")]
    (OUT / "counts.json").write_text(json.dumps({"units": counts, "subjects": dict(subjects), "years": years},
                                                ensure_ascii=False))
    print(f"單元題數 {len(counts):,} 筆；總題數 {sum(n for _, n in subjects):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
