#!/usr/bin/env python3
"""把「國中英語知識點對照表」網頁（逐點通，存成 HTML）轉成英語主題列表。

    python scripts/build_english_topics.py 章節對照.html -o data/curriculum/english_topics.yaml

表格每列是一個文法知識點，含所屬冊別與康軒、翰林、南一的課次。另外補上不屬於
單一文法點的綜合類（字彙、閱讀、對話、聽力…），讓這類題目不必硬塞進文法主題。
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

import yaml

BOOK = {1: (7, 1), 2: (7, 2), 3: (8, 1), 4: (8, 2), 5: (9, 1), 6: (9, 2)}
GENERAL = [
    ("V1", "字彙（詞義、拼字、詞性變化）"),
    ("V2", "片語與慣用語"),
    ("R1", "閱讀理解（短文、書信、圖表、廣告）"),
    ("R2", "對話與情境應答"),
    ("L1", "聽力"),
    ("G0", "其他文法（不在列表中的文法點）"),
]


def cell(s: str) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
    return "" if s in ("-", "－") else s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("page", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    args = ap.parse_args()

    s = args.page.read_text(encoding="utf-8")
    i = s.find("<table")
    table = s[i:s.find("</table>", i)]
    topics, book, n = [], 0, 0
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S):
        m = re.search(r">B(\d)<br", row)
        if m:
            book, n = int(m.group(1)), 0
        name = re.search(r'class="topic-name">(.*?)</td>', row, re.S)
        if not name or not book:
            continue
        pubs = dict(re.findall(r'class="pub-col (kang|han|nan)">(.*?)</td>', row, re.S))
        n += 1
        g, sem = BOOK[book]
        lessons = {pub: cell(pubs.get(k, "")) for pub, k in (("康軒", "kang"), ("翰林", "han"), ("南一", "nan"))}
        topics.append({"id": f"B{book}-{n:02d}", "title": cell(name.group(1)),
                       "group": f"B{book} {'七八九'[g - 7]}{'上下'[sem - 1]}",
                       "grade": g, "semester": sem,
                       "lessons": {k: v for k, v in lessons.items() if v}})
    topics += [{"id": k, "title": t, "group": "綜合"} for k, t in GENERAL]
    out = {"source": "逐點通「國中英語知識點對照表」（康版／翰版／南版，108 課綱）",
           "note": "B1–B6 為各冊文法知識點與三版本課次；綜合類為字彙、閱讀等非單一文法點的題目",
           "topics": topics}
    args.out.write_text(yaml.safe_dump(out, allow_unicode=True, sort_keys=False, width=200), encoding="utf-8")
    print(f"{len(topics)} 個主題 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
