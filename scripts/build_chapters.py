#!/usr/bin/env python3
"""依段考順序建構國文、英文的課文章節，寫回題目的 textbook 標籤。

國文、英文的章節就是課文。但考卷上很少逐題標出課次，卷頭的命題範圍
也常常沒印。能用的線索有兩個：

  1. 擷取時模型逐題判讀的課名（lesson_raw），依據是題目寫了課名、作者，
     或直接引用了課文原句 —— 只有能確定時才會有，字音字形、成語這類
     看不出出處的題目沒有。
  2. 段考順序本身。同一冊的課文是依序教的：第一次段考考前幾課，
     第三次段考考最後幾課。

所以跨所有考卷彙整「某一冊出現過哪些課名、各出現在第幾次段考」，
依段考次別的平均值排序，就能重建出每一冊的課文順序，不需要事先知道
各家出版社的目錄 —— 不同版本的課次編號不同，但課名在各版本間通用，
而老師檢索時用的也是課名。

判讀不出課名的題目歸到「第N冊 第K次段考範圍」：仍是有意義的章節分組，
而且不必猜。

用法:
    python scripts/build_chapters.py data/bank [--report out/chapters.md]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean

import yaml

SUBJECTS = {"國文", "英語"}
VOLUME = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六"}
TERM = {7: "七", 8: "八", 9: "九"}


def volume(doc: dict) -> int | None:
    g, s = doc.get("grade"), doc.get("semester")
    if g in (7, 8, 9) and s in (1, 2):
        return (g - 7) * 2 + s
    return None


def norm_title(t: str | None) -> str | None:
    """課名正規化：去掉書名號、引號、「第N課」前綴與空白，英文轉成首字大寫比對。"""
    if not t:
        return None
    t = re.sub(r"^(第\s*[一二三四五六七八九十\d]+\s*課|lesson\s*\d+|unit\s*\d+)[\s:：.、]*",
               "", t.strip(), flags=re.I)
    t = t.strip(" 　〈〉《》「」『』\"'“”‘’.。:：")
    return t or None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bank", type=Path)
    ap.add_argument("--report", type=Path, help="把各冊的課文清單寫成 Markdown")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    docs = []
    for p in sorted(args.bank.glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        m = d.get("document") or {}
        if m.get("subject") in SUBJECTS and volume(m):
            docs.append((p, d))

    # ── 彙整：每一冊出現過的課名，以及各自出現在第幾次段考 ──────────
    seen: dict[tuple[str, int], dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    display: dict[tuple[str, int, str], Counter] = defaultdict(Counter)
    for _, d in docs:
        m = d["document"]
        key = (m["subject"], volume(m))
        for q in d.get("questions") or []:
            raw = q.get("lesson_raw") or {}
            t = norm_title(raw.get("title"))
            if not t:
                continue
            k = t.lower() if m["subject"] == "英語" else t
            seen[key][k].append(m.get("exam_seq") or 0)
            display[(*key, k)][t] += 1

    catalog: dict[tuple[str, int], list[tuple[str, float, int]]] = {}
    for key, titles in seen.items():
        rows = [(display[(*key, k)].most_common(1)[0][0], mean(seqs), len(seqs))
                for k, seqs in titles.items()]
        # 依平均段考次別排序；同一段考內依出現次數（出現越多越可能是主課）
        catalog[key] = sorted(rows, key=lambda r: (r[1], -r[2]))

    # ── 寫回標籤 ──────────────────────────────────────────────
    changed = tagged = coarse = 0
    for p, d in docs:
        m = d["document"]
        vol = volume(m)
        known = {(r[0].lower() if m["subject"] == "英語" else r[0])
                 for r in catalog.get((m["subject"], vol), [])}
        dirty = False
        for q in d.get("questions") or []:
            t = norm_title((q.get("lesson_raw") or {}).get("title"))
            k = t.lower() if (t and m["subject"] == "英語") else t
            if t and k in known:
                tag = f"第{VOLUME[vol]}冊 {t}" if m["subject"] == "國文" else f"Book {vol} {t}"
                tagged += 1
            else:
                term = f"{TERM[m['grade']]}{'上' if m['semester'] == 1 else '下'}"
                tag = f"第{VOLUME[vol]}冊（{term}）第{m.get('exam_seq')}次段考範圍"
                coarse += 1
            tags = q.get("tags") or {}
            if tags.get("textbook") != tag:
                tags.update({"textbook": tag, "labeled_by": "ai"})
                q["tags"] = tags
                dirty = True
        if dirty and not args.dry_run:
            p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
            changed += 1

    print(f"{len(docs)} 份國文／英文卷；{tagged} 題標到課名，{coarse} 題標到段考範圍；更新 {changed} 份")
    lines = ["# 國文、英文課文章節（依段考順序重建）", "",
             "課名由擷取時的逐題判讀彙整而來，依出現的段考次別平均值排序。"
             "括號內是平均段考次別與出現題數。", ""]
    for (subj, vol), rows in sorted(catalog.items()):
        lines.append(f"## {subj} 第{VOLUME[vol]}冊")
        lines += [f"{i}. {t}（{avg:.1f}，{n} 題）" for i, (t, avg, n) in enumerate(rows, start=1)]
        lines.append("")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text("\n".join(lines), encoding="utf-8")
    for (subj, vol), rows in sorted(catalog.items()):
        print(f"  {subj} 第{VOLUME[vol]}冊：{len(rows)} 課 —— "
              + "、".join(r[0] for r in rows[:8]) + ("…" if len(rows) > 8 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
