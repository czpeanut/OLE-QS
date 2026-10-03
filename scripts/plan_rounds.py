#!/usr/bin/env python3
"""把整批考卷排成「輪次」：第一輪每場段考一份，第二輪每場段考再一份，依此類推。

目的是讓題庫的雛形盡早涵蓋所有組合。照目錄順序跑的話，跑了一整晚可能還
只有國一公民；分輪之後，第一輪跑完就已經是「每個年級 × 科目 × 學年度 ×
段考次別都至少有一份卷」的題庫，後面幾輪只是把每一格加厚。

「一場段考」＝ 年級／科目／學年度／學期-次數（不分學校）。
同一場段考裡挑卷的順序固定（依縣市、學校排序後輪流取不同縣市），
讓前幾輪盡量分散到不同地區，而不是先把同一個縣市的卷全部抓完。

用法:
    python scripts/plan_rounds.py 考卷根目錄 -o rounds.txt
    輸出一行一份 PDF（相對路徑），前面加上輪次，例如「1<TAB>國一/數學/113/1-1/台北/新興.pdf」
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--exclude", type=Path, help="已完成的清單（相對路徑一行一個），排除")
    args = ap.parse_args()

    done = set()
    if args.exclude and args.exclude.is_file():
        done = {l.strip() for l in args.exclude.read_text(encoding="utf-8").splitlines() if l.strip()}

    exams: dict[tuple, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for p in sorted(args.root.rglob("*.pdf")):
        rel = p.relative_to(args.root).as_posix()
        parts = rel.split("/")
        if len(parts) != 6 or rel in done:
            continue            # 年級／科目／學年度／學期-次數／縣市／學校.pdf
        grade, subject, year, exam, city, _ = parts
        exams[(grade, subject, year, exam)][city].append(rel)

    lines = []
    for key in sorted(exams):
        by_city = exams[key]
        # 輪流從不同縣市取卷
        queues = [sorted(v) for _, v in sorted(by_city.items())]
        order = []
        while any(queues):
            for q in queues:
                if q:
                    order.append(q.pop(0))
        for rnd, rel in enumerate(order, start=1):
            lines.append((rnd, key, rel))

    lines.sort(key=lambda x: (x[0], x[1]))
    args.out.write_text("".join(f"{r}\t{rel}\n" for r, _, rel in lines), encoding="utf-8")

    from collections import Counter
    per = Counter(r for r, _, _ in lines)
    print(f"{len(exams)} 場段考、{len(lines)} 份卷")
    for r in sorted(per)[:6]:
        print(f"  第 {r} 輪：{per[r]} 份")
    if len(per) > 6:
        print(f"  …共 {len(per)} 輪")
    return 0


if __name__ == "__main__":
    sys.exit(main())
