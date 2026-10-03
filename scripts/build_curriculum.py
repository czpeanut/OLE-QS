#!/usr/bin/env python3
"""下載並解析國中各版本（翰林、康軒、南一）教科書章節表，建成章節索引。

來源是升學王的「108 課綱國中版本對照表」：國一至國三、上下冊，國文、數學、
自然（七年級生物、八九年級理化、九年級地科）、社會（歷史、地理、公民）各一份 PDF，
每頁是一個版本的章節表（右半邊是對應的升學王自家課程，這裡不用）。
沒有英文。

PDF 裡的表格是文字層，直接讀表格結構，不經模型、不花錢：

    章 | 節次 | 節名          （數學、自然、社會；章那一格合併儲存格，往下沿用）
    課次 | 課名 | 群組(類別)   （國文）

「實驗 1-1」這類實驗列不列入索引（題目歸到它所屬的節）。

注意：對照表是 114、115 學年度的版本；題庫卷是 111～113 學年度，
個別課次在改版間可能有調動。

用法:
    python scripts/build_curriculum.py --cache out/curriculum_pdf \\
        -o data/curriculum/junior.yaml --report docs/curriculum.md
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
from pathlib import Path

import requests
import yaml

PAGE = "https://www.go100.com.tw/108_comparison_table.php"
BASE = "https://www.go100.com.tw/"

# 檔名 → (科目, 分科)
FILE_SUBJECT = {
    "數學": ("數學", None), "math": ("數學", None),
    "國文": ("國文", None), "chinese": ("國文", None),
    "生物": ("自然", "生物"), "biologic": ("自然", "生物"),
    "理化": ("自然", "理化"), "physics_and_chemistry": ("自然", "理化"),
    "地科": ("自然", "地科"), "geoscience": ("自然", "地科"),
    "歷史": ("社會", "歷史"), "history": ("社會", "歷史"),
    "地理": ("社會", "地理"), "geography": ("社會", "地理"),
    "公民": ("社會", "公民"), "civics": ("社會", "公民"),
}
PUBLISHERS = ("翰林", "康軒", "南一")
CN_GRADE = {"七": 7, "八": 8, "九": 9}
PUB_RE = re.compile(r"^(翰林|康軒|南一)([七八九])([上下])")
SEC_RE = re.compile(r"^\d+-\d+$")
CH_RE = re.compile(r"^第\s*(\d+)\s*章\s*(.*)$", re.S)


def clean(s: str | None) -> str:
    return re.sub(r"\s*\n\s*", "", s or "").strip()


def download(cache: Path) -> list[Path]:
    cache.mkdir(parents=True, exist_ok=True)
    html = requests.get(PAGE, headers={"User-Agent": "Mozilla/5.0"}, timeout=60).text
    links = sorted(set(re.findall(
        r'href="\.\./(file/108_(?:comparison_table|version_compare)/junior/[^"]+\.pdf)[^"]*"', html)))
    out = []
    for rel in links:
        dst = cache / Path(rel).name
        if not dst.is_file():
            r = requests.get(BASE + urllib.parse.quote(rel), headers={"User-Agent": "Mozilla/5.0"},
                             timeout=60)
            r.raise_for_status()
            dst.write_bytes(r.content)
        out.append(dst)
    return out


def subject_of(path: Path) -> tuple[str, str | None]:
    stem = path.stem
    for key, val in FILE_SUBJECT.items():
        if re.search(rf"(_|comparison_){re.escape(key)}(_\d+)?$", stem):
            return val
    raise ValueError(f"看不出科目：{path.name}")


def parse_pdf(path: Path) -> dict[tuple[str, int, int], list[dict]]:
    """回傳 {(版本, 年級, 學期): 單元列表}。"""
    import pymupdf

    subject, _ = subject_of(path)
    books: dict[tuple[str, int, int], list[dict]] = {}
    cur = None
    chapter = (None, "")
    for page in pymupdf.open(path):
        for table in page.find_tables().tables:
            for row in table.extract():
                cells = [clean(c) for c in row]
                m = PUB_RE.match(cells[0])
                if m:
                    cur = (m.group(1), CN_GRADE[m.group(2)], 1 if m.group(3) == "上" else 2)
                    books.setdefault(cur, [])
                    chapter = (None, "")
                    continue
                if cur is None or cells[0] in ("章", "課次") or cells[1] in ("節次", "節次", "課名"):
                    continue
                units = books[cur]
                if subject == "國文":
                    code, title, kind = cells[0], cells[1], cells[2]
                    if not code or not title:
                        continue
                    units.append({"order": len(units) + 1, "lesson": code, "title": title,
                                  "kind": kind or None})
                    continue
                if cells[0]:
                    cm = CH_RE.match(cells[0])
                    if cm:
                        chapter = (int(cm.group(1)), clean(cm.group(2)))
                sec, title = cells[1], cells[2]
                if not SEC_RE.match(sec or "") or not title:
                    continue                      # 實驗列、空列
                units.append({"chapter": chapter[0], "chapter_title": chapter[1],
                              "section": sec, "title": title})
    return books


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", type=Path, default=Path("out/curriculum_pdf"))
    ap.add_argument("-o", "--out", type=Path, default=Path("data/curriculum/junior.yaml"))
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()

    tree: dict = {}
    for pdf in download(args.cache):
        subject, sub = subject_of(pdf)
        for (pub, grade, sem), units in parse_pdf(pdf).items():
            book = (tree.setdefault(grade, {}).setdefault(sem, {})
                    .setdefault(subject, {}).setdefault(sub or "", {}))
            book[pub] = units

    doc = {"source": PAGE, "note": "108 課綱國中版本對照表（114、115 學年度版本）", "grades": tree}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=200),
                        encoding="utf-8")

    n = sum(len(u) for g in tree.values() for s in g.values() for sj in s.values()
            for sb in sj.values() for u in sb.values())
    print(f"{n} 個單元 → {args.out}")
    if args.report:
        args.report.write_text(report(tree), encoding="utf-8")
        print(f"報表 → {args.report}")
    return 0


GRADE_NAME = {7: "七年級（國一）", 8: "八年級（國二）", 9: "九年級（國三）"}


def report(tree: dict) -> str:
    out = ["# 國中各版本章節表",
           "",
           f"來源：[升學王 108 課綱國中版本對照表]({PAGE})（114、115 學年度版本）。"
           "由 `scripts/build_curriculum.py` 產生，資料在 `data/curriculum/junior.yaml`。",
           "英文不在對照表內，沿用考卷上的課名（`docs/chapters.md`）。", ""]
    for grade in sorted(tree):
        out.append(f"## {GRADE_NAME[grade]}")
        for sem in sorted(tree[grade]):
            for subject, subs in tree[grade][sem].items():
                for sub, pubs in subs.items():
                    name = f"{subject}・{sub}" if sub else subject
                    out.append(f"\n### {'上' if sem == 1 else '下'}學期　{name}\n")
                    pubs = {p: pubs[p] for p in PUBLISHERS if p in pubs}
                    cols = list(pubs)
                    out.append("| " + " | ".join(cols) + " |")
                    out.append("|" + "---|" * len(cols))
                    rows = max(len(u) for u in pubs.values())
                    for i in range(rows):
                        cells = []
                        for p in cols:
                            u = pubs[p][i] if i < len(pubs[p]) else None
                            if not u:
                                cells.append("")
                            elif "lesson" in u:
                                cells.append(f"{u['lesson']} {u['title']}")
                            else:
                                cells.append(f"{u['section']} {u['title']}")
                        out.append("| " + " | ".join(c.replace("|", "／") for c in cells) + " |")
        out.append("")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    sys.exit(main())
