#!/usr/bin/env python3
"""算出每題在選題頁的排序位置（question.sort_key）與所屬題組（question.parent_id＝題組第一題），
寫回本機題庫；--supabase 一併更新 Supabase。

    python scripts/build_sort_keys.py [--supabase] [--dry-run]

排列規則（各科相同）：
1. 作答形式：選擇題 → 填充題 → 應用題（計算、問答）→ 其他（是非、配合、題組）
   國文的純讀音、字義題（國字注音、注釋、「」中的字音／字形／字義比較）一律排在最後
2. 課本單元順序（依 data/curriculum 的章節表；查不到的放在該冊最後）
3. 題型目錄順序（數學，data/curriculum/math_patterns）
4. 難度由易到難（評過難度的科目）
5. 新的學年度在前，同卷依原題號
同一個題組（共用文章或圖）的題目排在一起，位置以題組第一題為準。

sort_key = 作答形式代號 × 10,000,000 + 名次；網頁用 sort_key // 10,000,000 標示「選擇題」「填充題」等段落。
Supabase 的 question 表要先有這個欄位：deploy/supabase/sort_key.sql
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from batch_api import _env  # noqa: E402

FORMAT_NAME = {0: "選擇題", 1: "填充題", 2: "應用題", 3: "其他題型", 9: "字音字義"}
STEP = 10_000_000
SUB_ORDER = {"生物": 0, "理化": 1, "地科": 2, "歷史": 0, "地理": 1, "公民": 2}

# 國文純讀音、字義題
DRILL_SECTION = re.compile(r"國字|注音|注釋|摘釋|解釋|字音|字形|形音義|字詞")
DRILL_STEM = re.compile(r"讀音|字音|注音|字形|錯別字|錯字|字義|詞義|意義(?:相同|不同)|解釋")
ZHUYIN = re.compile(r"[ㄅ-ㄩ˙ˊˇˋ]")


def is_drill(qtype: str, stem: str, section: str) -> bool:
    s = re.sub(r"\s", "", stem or "")
    if qtype in ("single", "multiple"):
        return len(s) <= 45 and bool(DRILL_STEM.search(s))
    # 短題目：一個詞或短句，配上國字注音／注釋類大題，或題幹本身帶注音、「」標記
    if len(s) <= 16 and (DRILL_SECTION.search(section or "") or ZHUYIN.search(s) or "「" in s):
        return True
    # 題幹自帶小標的，例如「(一)國字注音 4. 童「ㄙㄡˇ」無欺」「(二)注釋 2. 藤蔓：」
    return len(s) <= 40 and bool(ZHUYIN.search(s) or re.search(r"注音|注釋|字形|字音|解釋：", s))


def fmt_of(subject: str, qtype: str, grouped: bool) -> int:
    if grouped:
        return 3
    if qtype in ("single", "multiple"):
        return 0
    if qtype == "fill":
        return 1
    if qtype == "calc" or (qtype == "essay" and subject in ("數學", "自然")):
        return 2
    return 3


CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def natural(code: str) -> tuple:
    """「1-2」「B2-08」「第十二課」都能依數字排。"""
    def cn(m):
        t = m.group(0)
        if t == "十":
            return "10"
        if t.startswith("十"):
            return str(10 + CN[t[1]])
        if t.endswith("十"):
            return str(CN[t[0]] * 10)
        if len(t) == 3:
            return str(CN[t[0]] * 10 + CN[t[2]])
        return str(CN[t])
    s = re.sub(r"[一二三四五六七八九]?十[一二三四五六七八九]?|[一二三四五六七八九]", cn, code or "")
    return tuple(int(x) if x.isdigit() else x for x in re.findall(r"\d+|\D+", s))


def unit_orders() -> dict:
    """(科目, 年級, 學期, 子科, 版本, 代號) → 課本中的順序。"""
    junior = yaml.safe_load((REPO / "data/curriculum/junior.yaml").read_text(encoding="utf-8"))["grades"]
    out = {}
    for g, sems in junior.items():
        for sem, subjects in sems.items():
            for subject, subs in subjects.items():
                for sub, pubs in subs.items():
                    for pub, units in pubs.items():
                        for i, u in enumerate(units):
                            code = u.get("lesson") or u.get("section")
                            out[(subject, int(g), int(sem), sub or subject, pub, code)] = i
    topics = yaml.safe_load((REPO / "data/curriculum/english_topics.yaml").read_text(encoding="utf-8"))["topics"]
    for i, t in enumerate(topics):
        out[("英語", None, None, "英語", None, t["id"])] = i
    return out


def pattern_orders() -> dict[str, int]:
    out = {}
    for f in (REPO / "data/curriculum/math_patterns").glob("*.yaml"):
        cat = yaml.safe_load(f.read_text(encoding="utf-8"))
        for i, t in enumerate(cat["types"]):
            out[f"{f.stem}:{t['code']}"] = i
    return out


def compute(con: sqlite3.Connection) -> tuple[dict[str, int], dict[str, str]]:
    uo, po = unit_orders(), pattern_orders()
    sections = {(d, o): n for d, o, n in con.execute("select document_id, ord, name from section")}
    pattern = dict(con.execute("select question_id, value from tag where axis = 'pattern'"))
    rows = con.execute("""select q.id, d.subject, d.grade, d.semester, d.academic_year_roc, q.document_id, q.section_ord,
        q.number, q.type, q.stem_md, coalesce(q.group_stem, ''), q.shared_asset_key, q.unit_subject, q.unit_publisher,
        q.unit_code, q.difficulty from question q join document d on d.id = q.document_id""").fetchall()
    keys = {}
    blocks: dict[tuple, list] = defaultdict(list)
    for (qid, subject, g, sem, year, doc, sec, num, qtype, stem, gstem, shared, usub, upub, code, diff) in rows:
        grouped = bool(gstem.strip() or shared)
        if subject == "國文" and not grouped and is_drill(qtype, stem, sections.get((doc, sec), "")):
            fmt = 9
        else:
            fmt = fmt_of(subject, qtype, grouped)
        if subject == "英語":
            ur = uo.get(("英語", None, None, "英語", None, code), 999)
        else:
            sub = usub or subject
            ur = next((uo[k] for k in ((subject, g, sem, sub, upub, code), (subject, g, sem, sub, "翰林", code)) if k in uo), None)
            ur = 999 if ur is None else ur
        key = (subject, fmt, g or 0, sem or 0, SUB_ORDER.get(usub, 0), ur, natural(code or "~"),
               po.get(pattern.get(qid, ""), 99), diff or 0, -(year or 0), doc, sec, num)
        keys[qid] = key
        if grouped:                                    # 題組：同卷、同一段文章或同一張共用圖
            blocks[(doc, shared or gstem.strip()[:200])].append(qid)
    parent: dict[str, str] = {}
    for members in blocks.values():                   # 題組成員都用第一題的位置，再依題號排
        first = min(members, key=lambda i: (keys[i][-2], keys[i][-1]))
        for i in members:
            keys[i] = keys[first][:-3] + keys[i][-3:]
        for i in members:                             # parent_id 指向題組第一題，隨機選題時整組一起抽
            parent[i] = first
    order = sorted(keys, key=lambda i: keys[i])
    return {qid: keys[qid][1] * STEP + n for n, qid in enumerate(order, 1)}, parent


def push_supabase(con: sqlite3.Connection, sk: dict[str, int]) -> None:
    """只更新 sort_key：依值分批送 PATCH 太慢，改成整列 upsert（從本機題庫讀出完整的列）。"""
    url = _env("SUPABASE_URL").rstrip("/") + "/rest/v1"
    key = _env("SUPABASE_SERVICE_ROLE_KEY")
    H = {"Authorization": f"Bearer {key}", "apikey": key, "Content-Type": "application/json",
         "Prefer": "resolution=merge-duplicates,return=minimal"}
    cur = con.execute("select * from question")
    cols = [c[0] for c in cur.description]
    batch, n, t0 = [], 0, time.time()
    s = requests.Session()

    def send(b):
        for attempt in range(6):
            r = s.post(f"{url}/question", headers=H, data=json.dumps(b, ensure_ascii=False, default=str).encode(), timeout=300)
            if r.status_code in (200, 201, 204):
                return
            if r.status_code < 500 and r.status_code != 429:
                raise RuntimeError(f"{r.status_code} {r.text[:300]}")
            time.sleep(2 ** attempt)
        raise RuntimeError("上傳失敗")
    for row in cur:
        r = dict(zip(cols, row))
        for c in ("answer", "uncertain_spans"):
            if isinstance(r.get(c), str):
                r[c] = json.loads(r[c])
        r = {k: (v.replace("\x00", "") if isinstance(v, str) else v) for k, v in r.items()}
        batch.append(r)
        if len(batch) >= 1000:
            send(batch)
            n += len(batch)
            batch = []
    if batch:
        send(batch)
        n += len(batch)
    print(f"Supabase：更新 {n:,} 題，{time.time() - t0:.0f} 秒")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=REPO / "data/oleqs.db")
    ap.add_argument("--supabase", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    con = sqlite3.connect(args.db)
    sk, parent = compute(con)
    by = defaultdict(lambda: defaultdict(int))
    subj = dict(con.execute("select q.id, d.subject from question q join document d on d.id = q.document_id"))
    for qid, v in sk.items():
        by[subj[qid]][FORMAT_NAME[v // STEP]] += 1
    for s, c in by.items():
        print(s, dict(c))
    if args.dry_run:
        return 0
    if "sort_key" not in [r[1] for r in con.execute("pragma table_info(question)")]:
        con.execute("alter table question add column sort_key integer")
        con.execute("create index if not exists ix_question_sort_key on question (sort_key)")
    con.executemany("update question set sort_key = ?, parent_id = ? where id = ?",
                    [(v, parent.get(k), k) for k, v in sk.items()])
    con.commit()
    print(f"本機題庫：{len(sk):,} 題")
    if args.supabase:
        push_supabase(con, sk)
    return 0


if __name__ == "__main__":
    sys.exit(main())
