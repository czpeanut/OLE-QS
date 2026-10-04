#!/usr/bin/env python3
"""數學「題型目錄」與難度：由 AI 歸納每個單元的題型，再把題目歸類並評難度。

    # 1. 歸納題型（每單元一份目錄草稿，老師審過後再用）
    python scripts/math_patterns.py discover 7-1:3-3 8-1:2-3 9-1:1-4
    # 2. 歸類並評難度（--sample 只抽部分題目；--runs 2 評兩次看穩定度）
    python scripts/math_patterns.py classify 7-1:3-3 --sample 120 --runs 2

單元寫成「年級-學期:單元代號」，例如 8-1:2-3＝八上 2-3。
- 題型目錄：data/curriculum/math_patterns/<年級-學期>_<單元>.yaml（status: draft 表示還沒審）
- 歸類結果：out/math_patterns/<年級-學期>_<單元>.json（試跑期間不寫回題庫）
- 題目附圖與題組附圖會一起送給模型（每題最多 2 張），題組子題附上題組前文
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import batch_api as B  # noqa: E402

os.environ.setdefault("GEMINI_API_KEY", B._env("GEMINI_API_KEY"))
from vlm_extract import call_model  # noqa: E402

MODEL = "gemini-3.7-flash"
PHASE = "math_patterns"
CATALOG_DIR = REPO / "data/curriculum/math_patterns"
OUT_DIR = REPO / "out/math_patterns"
ASSETS = REPO / "data/assets"
GRADE_NAME = {7: "七", 8: "八", 9: "九"}

RUBRIC = """難度 1～5，以該年級學生在段考的表現來判斷（不考慮作答形式是選擇或填充）：
1＝直接套用定義或公式，一步完成
2＝兩三步的基本計算或單一概念的標準題
3＝需要轉換題意、組合兩個概念，或一般的應用題
4＝多步推理、需要輔助線、設未知數列式或分類討論
5＝段考壓軸題，需要巧思或較長的推理"""


def parse_unit(s: str) -> tuple[int, int, str]:
    gs, code = s.split(":")
    g, sem = gs.split("-")
    return int(g), int(sem), code


def unit_key(g: int, sem: int, code: str) -> str:
    return f"{g}-{sem}_{code}"


def load_unit(con: sqlite3.Connection, g: int, sem: int, code: str) -> list[dict]:
    rows = con.execute("""
        select q.id, q.type, q.stem_md, q.group_stem, q.shared_asset_key, q.document_id, q.number,
               q.answer_source, q.unit_title,
               (select max(q2.number) from question q2 where q2.document_id = q.document_id) as max_no
        from question q join document d on d.id = q.document_id
        where d.subject = '數學' and d.grade = ? and d.semester = ? and q.unit_code = ? and q.stem_md != ''
        order by q.id""", (g, sem, code)).fetchall()
    cols = ["id", "type", "stem", "group_stem", "shared_key", "doc", "number", "answer_source", "unit_title", "max_no"]
    qs = [dict(zip(cols, r)) for r in rows]
    for q in qs:
        figs = [r[0] for r in con.execute(
            "select file from asset where question_id = ? and file is not null and kind != 'table' order by key", (q["id"],))]
        if q["shared_key"]:
            figs = [r[0] for r in con.execute(
                "select file from asset where document_id = ? and key = ? and file is not null",
                (q["doc"], q["shared_key"]))] + figs
        q["figs"] = figs[:2]
        q["short"] = q["id"].split("_", 2)[-1]          # 送模型的代號，去掉 doc_ 前綴省 token
    return qs


def norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", s or "")[:80]


def text_of(q: dict, limit: int = 300) -> str:
    head = f"[{q['short']}]（{q['type']}{'，有附圖' if q['figs'] else ''}）"
    ctx = f"〔題組前文〕{q['group_stem'][:200]}〔本題〕" if q["group_stem"] else ""
    return head + " " + ctx + (q["stem"] or "")[:limit]


# ── 歸納題型 ──
DISCOVER_SCHEMA = {"type": "object", "properties": {"types": {"type": "array", "items": {"type": "object", "properties": {
    "code": {"type": "string"}, "name": {"type": "string"}, "desc": {"type": "string"},
    "examples": {"type": "array", "items": {"type": "string"}}}, "required": ["code", "name", "desc"]}}},
    "required": ["types"]}


def discover(con: sqlite3.Connection, unit: str, n: int, force: bool) -> None:
    g, sem, code = parse_unit(unit)
    dest = CATALOG_DIR / f"{unit_key(g, sem, code)}.yaml"
    if dest.exists() and not force:
        print(f"{dest.name} 已存在，略過（--force 重做）")
        return
    qs = load_unit(con, g, sem, code)
    seen, uniq = set(), []
    for q in qs:                                       # 重複出現的題目只留一份
        k = norm(q["stem"])
        if k not in seen:
            seen.add(k)
            uniq.append(q)
    random.Random(1).shuffle(uniq)
    sample = uniq[:n]
    title = qs[0]["unit_title"] if qs else code
    prompt = (f"你是國中數學老師，要替題庫建立「題型目錄」。以下是{GRADE_NAME[g]}年級{'上下'[sem - 1]}學期"
              f"「{code} {title}」單元的 {len(sample)} 題段考題（抽樣）。\n"
              "請歸納出這個單元的題型（8～15 個）。每個題型是老師出卷時會想挑的一類題目，同一題型的題目解題核心相同；"
              "課本例題常見的類型都要涵蓋，必要時拆細。題型要互斥、粒度一致，不要以選擇／填充等作答形式區分。\n"
              "每個題型給：code（T01 起）、name（12 字內）、desc（判斷標準一句話）、examples（2～3 個題目代號）。\n\n"
              + "\n".join(text_of(q) for q in sample))
    out, model, usd = call_model([{"text": prompt}], MODEL, schema=DISCOVER_SCHEMA, fallback=[MODEL], thinking="medium")
    twd = B.Ledger.add(PHASE, usd)
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    doc = {"unit": {"grade": g, "semester": sem, "code": code, "title": title},
           "status": "draft", "model": model, "sampled": len(sample), "unit_questions": len(qs),
           "types": [{"code": t["code"], "name": t["name"], "desc": t["desc"]} for t in out["types"]]}
    dest.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=200), encoding="utf-8")
    print(f"{dest.name}：{len(out['types'])} 個題型，US${usd:.4f}｜階段累計 NT${twd:.0f}")


# ── 歸類與難度 ──
CLASSIFY_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "id": {"type": "string"}, "type_code": {"type": "string"}, "difficulty": {"type": "integer"},
    "steps": {"type": "integer"}, "reason": {"type": "string"}},
    "required": ["id", "type_code", "difficulty", "steps", "reason"]}}}, "required": ["items"]}


def classify_chunk(catalog: dict, chunk: list[dict]) -> tuple[list[dict], float]:
    u = catalog["unit"]
    types = "\n".join(f"{t['code']} {t['name']}：{t['desc']}" for t in catalog["types"])
    parts: list[dict] = [{"text": (
        f"你是國中數學老師。下列是{GRADE_NAME[u['grade']]}年級「{u['code']} {u['title']}」單元的題目。"
        "依題型目錄替每題歸類（選 1 個最主要的題型；都不符合填 OTHER），並評難度。\n"
        + RUBRIC + "\nsteps＝解題主要步驟數；reason＝20 字內的難度理由。附圖緊接在該題文字之後。\n\n題型目錄：\n" + types
        + "\n\n題目：")}]
    for q in chunk:
        parts.append({"text": "\n" + text_of(q, 500)})
        for f in q["figs"]:
            p = ASSETS / f
            if p.is_file():
                parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(p.read_bytes()).decode()}})
    out, _, usd = call_model(parts, MODEL, schema=CLASSIFY_SCHEMA, fallback=[MODEL], thinking="low")
    return out["items"], usd


def classify(con: sqlite3.Connection, unit: str, sample: int | None, runs: int, chunk: int, workers: int) -> None:
    g, sem, code = parse_unit(unit)
    key = unit_key(g, sem, code)
    catalog = yaml.safe_load((CATALOG_DIR / f"{key}.yaml").read_text(encoding="utf-8"))
    qs = load_unit(con, g, sem, code)
    if sample:
        qs = random.Random(2).sample(qs, min(sample, len(qs)))
    by_short = {q["short"]: q for q in qs}
    result: dict[str, dict] = {q["id"]: {"runs": []} for q in qs}
    total = 0.0
    for run in range(runs):
        order = qs[:]
        random.Random(100 + run).shuffle(order)       # 每次換順序，避免評分受前後題影響
        chunks = [order[i:i + chunk] for i in range(0, len(order), chunk)]
        with ThreadPoolExecutor(workers) as pool:
            for items, usd in pool.map(lambda c: classify_chunk(catalog, c), chunks):
                total += usd
                for it in items:
                    q = by_short.get(it["id"])
                    if q:
                        result[q["id"]]["runs"].append({k: it.get(k) for k in ("type_code", "difficulty", "steps", "reason")})
    twd = B.Ledger.add(PHASE, total)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    meta = {q["id"]: {"type": q["type"], "answer_source": q["answer_source"], "figs": q["figs"],
                      "position": round(q["number"] / q["max_no"], 3) if q["max_no"] else None} for q in qs}
    (OUT_DIR / f"{key}.json").write_text(json.dumps({"unit": catalog["unit"], "results": result, "meta": meta},
                                                    ensure_ascii=False, indent=1), encoding="utf-8")
    done = sum(1 for r in result.values() if len(r["runs"]) == runs)
    print(f"{key}：{done}/{len(qs)} 題完成 {runs} 次評分，US${total:.4f}｜階段累計 NT${twd:.0f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["discover", "classify"])
    ap.add_argument("units", nargs="+")
    ap.add_argument("--db", type=Path, default=REPO / "data/oleqs.db")
    ap.add_argument("--discover-n", type=int, default=200)
    ap.add_argument("--sample", type=int)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--chunk", type=int, default=20)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    for u in args.units:
        if args.action == "discover":
            discover(con, u, args.discover_n, args.force)
        else:
            classify(con, u, args.sample, args.runs, args.chunk, args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
