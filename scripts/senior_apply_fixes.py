#!/usr/bin/env python3
"""把執行 agent 手動修正的高中暫緩題（REVISED_QUESTIONS_ONLY.jsonl）審核後寫回 data/high_school/bank。

  python scripts/senior_apply_fixes.py <REVISED_QUESTIONS_ONLY.jsonl> --sources <合庫 sources 資料夾>

審核規則（結果寫在 data/high_school/fix_review.csv，每題一行）：
- 新增的文字（題幹、題組、選項裡多出來的部分）要在原卷 PDF 找得到：
  比對新增文字的 4 字片段，60% 以上出現在原卷才收；找不到或沒有原卷可比對的不收（避免補造的表格、數據、圖說、文章）。
- 新增文字少於 15 字的小幅修正（錯字、亂碼、排版、移除混入的他題文字）收。
- 為了配合答案而改題目的不收。
收下的題：
- 題幹、題組、選項文字、題型、解析照修正版；選項附圖照原本的代號保留
- 原本沒答案、修正版補上的：answer_status＝ai_generated、answer_source＝agent
- 修正版改掉答案卷答案的：answer_status＝disputed、answer_source＝agent_fix，原答案記在 review_note
"""
import argparse, csv, difflib, json, re, sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
BANK = REPO / "data/high_school/bank"
OUT = REPO / "data/high_school/fix_review.csv"
FIT_ANSWER = re.compile(r"以符合|符合.{0,6}(答案|解答|解析)|使.{0,8}(成立|有解)|題意唯一成立|修正題幹.{0,20}(敘述|瑕疵)")


def norm(s):
    return re.sub(r"\$[^$]*\$|<[^>]+>", "", s or "")


def text(q):
    return "\n".join([q.get("group_stem") or "", q.get("stem") or ""] + [o.get("content") or "" for o in q.get("options") or []])


def dump(d, p):
    p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False, width=10_000), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("revised", type=Path)
    ap.add_argument("--sources", type=Path, required=True)
    a = ap.parse_args()
    import fitz
    R = {json.loads(l)["id"]: json.loads(l) for l in a.revised.read_text(encoding="utf-8").splitlines() if l.strip()}
    pdf_cache = {}

    def pdftext(sf):
        if sf not in pdf_cache:
            p = a.sources / sf
            pdf_cache[sf] = (re.sub(r"[^一-鿿0-9A-Za-z]", "", "".join(pg.get_text() for pg in fitz.open(p)))
                             if p.is_file() else None)
        return pdf_cache[sf]

    rows, acc = [], 0
    for y in sorted(BANK.glob("*.yaml")):
        d = yaml.safe_load(y.read_text())
        sf = d["document"]["source_file"]
        dirty = False
        for q in d["questions"]:
            r = R.get(q["id"])
            if not r:
                continue
            note = r.get("source", "")
            note = note[note.find("（"):] if "（" in note else note
            old, new = norm(text(q)), norm(text(r))
            sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
            added = re.sub(r"[^一-鿿0-9A-Za-z]", "",
                           "".join(new[j1:j2] for t, _, _, j1, j2 in sm.get_opcodes() if t in ("insert", "replace")))
            if FIT_ANSWER.search(note):
                rows.append((q["id"], "不收", "為配合答案改題目", note)); continue
            if len(added) >= 15:
                pt = pdftext(sf)
                if not pt:
                    rows.append((q["id"], "不收", f"新增 {len(added)} 字，交件包沒有原卷可比對", note)); continue
                grams = [added[k:k + 4] for k in range(0, len(added) - 3, 2)]
                hit = sum(g in pt for g in grams) / len(grams)
                if hit < 0.6:
                    rows.append((q["id"], "不收", f"新增 {len(added)} 字只有 {hit:.0%} 在原卷找得到（疑似補造）", note)); continue
            # 收下
            assets = {o["label"]: o.get("asset") for o in q.get("options") or []}
            q["stem"] = r["stem"]
            if r.get("group_stem"):
                q["group_stem"] = r["group_stem"]
            else:
                q.pop("group_stem", None)
            opts = []
            for o in r.get("options") or []:
                x = {"label": o["label"], "content": o.get("content") or ""}
                if assets.get(o["label"]):
                    x["asset"] = assets[o["label"]]
                opts.append(x)
            if opts:
                q["options"] = opts
            else:
                q.pop("options", None)
            q["type"] = r["type"]
            if r.get("explanation"):
                q["explanation"] = r["explanation"]
            ra, ba = r.get("answer"), q.get("answer")
            if ra and ra != ba:
                if not ba:
                    q["answer_status"], q["answer_source"] = "ai_generated", "agent"
                else:
                    q["review_note"] = f"原答案卷答案 {ba}，執行 agent 修正為 {ra}"
                    q["answer_status"], q["answer_source"] = "disputed", "agent_fix"
                q["answer"] = ra
            tags = q.setdefault("tags", {})
            tags["fixed_by"] = "agent"
            tags["fix_note"] = note
            dirty = True
            acc += 1
            rows.append((q["id"], "收", "", note))
        if dirty:
            dump(d, y)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["題目id", "審核", "原因", "agent 修正說明"])
        w.writerows(sorted(rows))
    rej = sum(1 for r in rows if r[1] == "不收")
    print(f"修正 {len(R):,} 題：收 {acc:,}、不收 {rej:,}（{OUT.relative_to(REPO)}）；對不到題庫 {len(R) - len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
