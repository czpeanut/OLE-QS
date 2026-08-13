#!/usr/bin/env python3
"""列出「還缺答案卷」的考卷，依學校整理，供人去把答案卷找回來。

答案是題庫目前最大的缺口，而補答案最可靠的方式不是讓模型作答，
是去把原本就存在的答案卷找回來 —— 那同時也是日後量測 AI 作答準確率的
校準資料。這份報表就是那件事的工作清單。

只算通過品管閘門的題目：被剔除的題目不會出現在檢索與組卷裡，
替它們找答案沒有意義。

用法:
    python scripts/report_missing_answers.py                 # 印到終端
    python scripts/report_missing_answers.py -o out/wanted.md
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.models import split_school  # noqa: E402

QUERY = """
select d.school, d.academic_year_roc, d.semester, d.exam_seq, d.subject,
       count(*)                                        as kept,
       sum(q.answer_status = 'missing')                as missing
  from question q
  join document d on d.id = q.document_id
 where q.status = 'reviewed'
 group by d.id
 order by d.school, d.academic_year_roc, d.semester, d.exam_seq
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", type=Path)
    ap.add_argument("--db", default=os.environ.get("OLEQS_DB", "data/oleqs.db"))
    args = ap.parse_args()

    rows = sqlite3.connect(args.db).execute(QUERY).fetchall()

    by_school: dict[str, list] = defaultdict(list)
    for school, year, sem, seq, subject, kept, missing in rows:
        if missing:
            by_school[school].append((year, sem, seq, subject, kept, missing))

    ranked = sorted(by_school.items(), key=lambda kv: -sum(r[5] for r in kv[1]))
    n_papers = sum(len(v) for v in by_school.values())
    n_missing = sum(r[5] for v in by_school.values() for r in v)

    out: list[str] = ["# 還缺答案卷的考卷", ""]
    out.append(f"{len(ranked)} 所學校、{n_papers} 份卷、**{n_missing} 題**沒有答案。")
    out.append("")
    out.append("找答案卷時用「學校＋學年度＋第幾學期第幾次段考」去查最準；"
               "同一次段考的答案卷通常和題目卷放在一起。")
    out.append("")
    out.append("| 學校 | 縣市 | 缺答案的卷 | 缺答案題數 |")
    out.append("|---|---|---:|---:|")
    for school, items in ranked:
        city, _ = split_school(school)
        out.append(f"| {school} | {city} | {len(items)} | {sum(i[5] for i in items)} |")

    out += ["", "## 逐校明細", ""]
    for school, items in ranked:
        city, short = split_school(school)
        out.append(f"### {school}（{city} {short}）")
        out.append("")
        for year, sem, seq, subject, kept, missing in items:
            got = kept - missing
            note = f"（已有 {got} 題答案）" if got else ""
            out.append(f"- {year} 學年度　第 {sem} 學期第 {seq} 次段考　{subject}　"
                       f"缺 {missing}／{kept} 題{note}")
        out.append("")

    # 已經齊全的卷也要列出來，不然找的人會重複去翻已經有答案的那幾份
    done = [(s, y, sem, seq, subj, kept) for s, y, sem, seq, subj, kept, missing in rows
            if not missing]
    if done:
        out += ["## 已經有完整答案，不必再找", ""]
        for school, year, sem, seq, subject, kept in done:
            out.append(f"- {school}　{year} 學年度第 {sem} 學期第 {seq} 次　{subject}　{kept} 題")
        out.append("")

    text = "\n".join(out)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"寫入 {args.out}（{len(ranked)} 所學校、{n_papers} 份卷、{n_missing} 題）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
