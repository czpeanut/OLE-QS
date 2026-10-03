#!/usr/bin/env python3
"""題庫統計：總題數、答案狀態、各章節題數，輸出成 Markdown。

    python scripts/bank_stats.py data/bank -o docs/bank_stats.md --units docs/unit_counts.md

只算通過品管閘門的題目。章節沿用 tags.chapter：數學、自然各版本節次大致對齊，合併三版本；
國文、社會各版本課次不同，列翰林並另計康軒、南一（看不出版本時模型偏向判成翰林）。
"""

from __future__ import annotations

import argparse
import collections
import sys
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from apps.api.quality import evaluate  # noqa: E402

SUBJECTS = ["國文", "英語", "數學", "自然", "社會"]
ANS = ["答案卷", "AI 兩模型一致", "AI 三票兩票", "待判定", "無答案"]
TERM = {7: "七", 8: "八", 9: "九"}
MERGED = ("數學", "生物", "理化", "地科")


def answer_kind(q: dict) -> str:
    st, src = q.get("answer_status"), str(q.get("answer_source") or "")
    if st == "disputed":
        return "待判定"
    if src == "ai:gemini+qwen":
        return "AI 兩模型一致"
    if src.startswith("ai:"):
        return "AI 三票兩票"
    return "答案卷" if q.get("answer") else "無答案"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bank", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--units", type=Path, required=True)
    ap.add_argument("--curriculum", type=Path, default=Path("data/curriculum/junior.yaml"))
    ap.add_argument("--topics", type=Path, default=Path("data/curriculum/english_topics.yaml"))
    args = ap.parse_args()

    docs = collections.Counter()
    total = collections.Counter()
    kept = collections.Counter()
    by_grade = collections.Counter()
    answers = collections.Counter()
    types = collections.Counter()
    chap = collections.Counter()
    units = collections.Counter()
    for p in sorted(args.bank.glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        m = d["document"]
        s = m["subject"]
        docs[s] += 1
        for q in d.get("questions") or []:
            total[s] += 1
            if not evaluate(q, d)[0]:
                continue
            kept[s] += 1
            by_grade[(s, m["grade"])] += 1
            answers[(s, answer_kind(q))] += 1
            types[(s, q.get("type"))] += 1
            c = (q.get("tags") or {}).get("chapter") or {}
            if c.get("code"):
                chap[s] += 1
                units[(m["grade"], m["semester"], c["subject"], c["publisher"], c["code"])] += 1

    subs = [s for s in SUBJECTS if s in docs]
    out = [f"# 題庫統計（{time.strftime('%Y-%m-%d')}）", "",
           "只算通過品管閘門的題目。由 `scripts/bank_stats.py` 產生。", "",
           "## 總覽", "",
           "| 科目 | 考卷 | 題目 | 收錄 | 國一 | 國二 | 國三 | 有章節 |",
           "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for s in subs:
        out.append(f"| {s} | {docs[s]:,} | {total[s]:,} | {kept[s]:,} | "
                   + " | ".join(f"{by_grade[(s, g)]:,}" for g in (7, 8, 9))
                   + f" | {chap[s]:,} |")
    out.append(f"| **合計** | **{sum(docs.values()):,}** | **{sum(total.values()):,}** | "
               f"**{sum(kept.values()):,}** | "
               + " | ".join(f"**{sum(by_grade[(s, g)] for s in subs):,}**" for g in (7, 8, 9))
               + f" | **{sum(chap.values()):,}** |")
    out += ["", "## 答案", "",
            "| 科目 | " + " | ".join(ANS) + " |", "|---|" + "---:|" * len(ANS)]
    for s in subs:
        out.append(f"| {s} | " + " | ".join(f"{answers[(s, a)]:,}" for a in ANS) + " |")
    out.append("| **合計** | " + " | ".join(f"**{sum(answers[(s, a)] for s in subs):,}**" for a in ANS) + " |")
    out += ["", "「無答案」包含問答、計算、配合題（不自動作答）與還在等第三票的卷。", ""]
    args.out.write_text("\n".join(out) + "\n", encoding="utf-8")

    # ── 各單元題數 ──
    curr = yaml.safe_load(args.curriculum.read_text(encoding="utf-8"))["grades"]
    u = ["# 各單元題數", "", f"由 `scripts/bank_stats.py` 產生（{time.strftime('%Y-%m-%d')}），只算通過品管閘門的題目。",
         "數學、自然各版本節次大致對齊，合併三版本計數，節名列翰林的；國文、社會列翰林的單元，"
         "並另計康軒、南一（模型看不出版本時偏向判成翰林）。英語依內容主題分類，不分版本。", ""]
    for name in ("數學", "生物", "理化", "地科", "國文", "歷史", "地理", "公民"):
        u += [f"## {name}", ""]
        for g in sorted(curr):
            for sem in sorted(curr[g]):
                for subject, subsubs in curr[g][sem].items():
                    for sub, pubs in subsubs.items():
                        if (sub or subject) != name:
                            continue
                        book = f"{TERM[g]}{'上' if sem == 1 else '下'}"
                        if name in MERGED:
                            seen: dict[str, str] = {}
                            for pub in ("翰林", "康軒", "南一"):
                                for x in pubs.get(pub) or []:
                                    seen.setdefault(x["section"], x["title"])
                            rows = [(code, title, sum(units[(g, sem, name, p, code)] for p in ("翰林", "康軒", "南一")))
                                    for code, title in sorted(seen.items(), key=lambda kv: [int(n) for n in kv[0].split("-")])]
                            tot = sum(r[2] for r in rows)
                            u += [f"### {book}（{tot:,} 題）", "", "| 節 | 節名 | 題數 |", "|---|---|---:|"]
                            u += [f"| {c} | {t} | {n:,} |" for c, t, n in rows]
                        else:
                            other = {p: sum(v for k, v in units.items() if k[:4] == (g, sem, name, p))
                                     for p in ("康軒", "南一")}
                            rows = []
                            for x in pubs.get("翰林") or []:
                                code = x.get("lesson") or x["section"]
                                rows.append((code, x["title"], units[(g, sem, name, "翰林", code)]))
                            tot = sum(r[2] for r in rows)
                            u += [f"### {book}（翰林 {tot:,} 題；康軒另計 {other['康軒']:,}、南一 {other['南一']:,}）", "",
                                  "| 課／節 | 名稱 | 題數 |", "|---|---|---:|"]
                            u += [f"| {c} | {t.replace('|', '／')} | {n:,} |" for c, t, n in rows]
                        u.append("")
    if args.topics.is_file():
        topics = yaml.safe_load(args.topics.read_text(encoding="utf-8"))["topics"]
        en = {(g, sem): {k[4]: v for k, v in units.items() if k[:3] == (g, sem, "英語")}
              for g in (7, 8, 9) for sem in (1, 2)}
        cols = [(g, sem) for g in (7, 8, 9) for sem in (1, 2)]
        u += ["## 英語（依內容主題，不分版本）", "",
              "| 代號 | 分組 | 主題 | " + " | ".join(f"{TERM[g]}{'上' if s == 1 else '下'}" for g, s in cols)
              + " | 合計 |", "|---|---|---|" + "---:|" * (len(cols) + 1)]
        for t in topics:
            ns = [en[c].get(t["id"], 0) for c in cols]
            u.append(f"| {t['id']} | {t.get('group') or ''} | {t['title']} | "
                     + " | ".join(f"{n:,}" for n in ns) + f" | {sum(ns):,} |")
        u.append("")
    args.units.write_text("\n".join(u) + "\n", encoding="utf-8")
    print(f"→ {args.out}、{args.units}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
