#!/usr/bin/env python3
"""數學「題型目錄」與難度：由 AI 歸納每個單元的題型，再把題目歸類並評難度。

    # 1. 歸納題型（每單元一份目錄；--batch 走批次 API 半價）
    python scripts/math_patterns.py discover 8-1:2-3 [--batch]
    python scripts/math_patterns.py discover all --batch
    # 2. 歸類並評難度
    python scripts/math_patterns.py classify all --batch
    # 3. 修整目錄：太雜的題型拆開、「歸不進」的題目補新題型，受影響的題目重新歸類
    python scripts/math_patterns.py refine all
    # 4. 看結果
    python scripts/math_patterns.py report all

單元寫成「年級-學期:單元代號」，例如 8-1:2-3＝八上 2-3；all＝全部數學單元。
- 題型目錄：data/curriculum/math_patterns/<年級-學期>_<單元>.yaml
- 歸類結果：out/math_patterns/<年級-學期>_<單元>.json（寫回題庫另外做）
- 題目附圖與題組附圖會一起送給模型（每題最多 2 張）；題組子題附上題組前文，「承上題」附上前一題
"""

from __future__ import annotations

import argparse
import base64
import collections
import json
import os
import random
import re
import sqlite3
import sys
import time
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
MIN_UNIT = 30                 # 題數太少的單元（多半是歸錯的零星題）不建目錄
SPLIT_SHARE = 0.22            # 一個題型超過單元題數的這個比例就拆
OTHER_SHARE = 0.06            # 「歸不進」超過這個比例就補題型

PRINCIPLES = """題型的原則：
1. 依「解題核心＋題目情境或圖形結構」分，不依作答形式（選擇、填充、計算），也不依「只要列式」或「要求出答案」分。
2. 用老師熟悉的名稱；課本與參考書常見的經典題型要獨立成類（例如年齡問題、盈不足、雞兔同籠、A 字型與 X 字型、母子相似、影長測高）。
3. 粒度一致：每個題型預期佔本單元 3%～20% 的題目；內容太雜的要依情境或圖形結構拆開，太細的合併。
4. 題型之間互斥；desc 要寫出判斷標準，讓人能據此把題目歸類。
5. 各版本教科書的同一單元內容可能略有不同（例如某版本多教一節），都要涵蓋。"""

RUBRIC = """難度 1～5，以該年級學生在段考的表現來判斷（不考慮作答形式是選擇或填充）：
1＝直接套用定義或公式，一步完成
2＝兩三步的基本計算或單一概念的標準題
3＝需要轉換題意、組合兩個概念，或一般的應用題
4＝多步推理、需要輔助線、設未知數列式或分類討論
5＝段考壓軸題，需要巧思或較長的推理"""


# ─────────────────────────── 題目 ───────────────────────────

def parse_unit(s: str) -> tuple[int, int, str]:
    gs, code = s.split(":")
    g, sem = gs.split("-")
    return int(g), int(sem), code


def unit_key(g: int, sem: int, code: str) -> str:
    return f"{g}-{sem}_{code}"


def all_units(con: sqlite3.Connection) -> list[str]:
    rows = con.execute("""select d.grade, d.semester, q.unit_code, count(*) from question q
        join document d on d.id = q.document_id
        where d.subject = '數學' and q.unit_code is not null group by 1, 2, 3 order by 1, 2, 3""")
    return [f"{g}-{s}:{c}" for g, s, c, n in rows if n >= MIN_UNIT]


def semester_units(con: sqlite3.Connection, g: int, sem: int) -> list[tuple[str, str]]:
    """本學期各單元（代號, 最常見的名稱），讓模型判斷題目真正屬於哪個單元。"""
    rows = con.execute("""select q.unit_code, q.unit_title, count(*) from question q join document d on d.id = q.document_id
        where d.subject = '數學' and d.grade = ? and d.semester = ? and q.unit_code is not null
        group by 1, 2 order by 1, 3 desc""", (g, sem)).fetchall()
    out: dict[str, str] = {}
    total = collections.Counter()
    for c, t, n in rows:
        out.setdefault(c, t)
        total[c] += n
    return [(c, out[c]) for c in out if total[c] >= MIN_UNIT]


def expand(con: sqlite3.Connection, units: list[str]) -> list[str]:
    return all_units(con) if units == ["all"] else units


def load_unit(con: sqlite3.Connection, g: int, sem: int, code: str) -> list[dict]:
    rows = con.execute("""
        select q.id, q.type, q.stem_md, q.group_stem, q.shared_asset_key, q.document_id, q.section_ord,
               q.number, q.answer_source, q.unit_title,
               (select max(q2.number) from question q2 where q2.document_id = q.document_id) as max_no
        from question q join document d on d.id = q.document_id
        where d.subject = '數學' and d.grade = ? and d.semester = ? and q.unit_code = ? and q.stem_md != ''
        order by q.id""", (g, sem, code)).fetchall()
    cols = ["id", "type", "stem", "group_stem", "shared_key", "doc", "sec", "number", "answer_source",
            "unit_title", "max_no"]
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
        q["prev"] = None
        if re.match(r"\s*(?:\(\d\)\s*)?承", q["stem"] or "") or "承上題" in (q["stem"] or ""):
            r = con.execute("select stem_md from question where document_id = ? and section_ord = ? and number = ?",
                            (q["doc"], q["sec"], q["number"] - 1)).fetchone()
            q["prev"] = r[0] if r else None
    return qs


def norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", s or "")[:80]


def text_of(q: dict, limit: int = 300) -> str:
    head = f"[{q['short']}]（{q['type']}{'，有附圖' if q['figs'] else ''}）"
    ctx = ""
    if q["group_stem"]:
        ctx += f"〔題組前文〕{q['group_stem'][:200]}"
    if q["prev"]:
        ctx += f"〔前一題〕{q['prev'][:200]}"
    return head + " " + (ctx + "〔本題〕" if ctx else "") + (q["stem"] or "")[:limit]


def image_parts(q: dict) -> list[dict]:
    out = []
    for f in q["figs"]:
        p = ASSETS / f
        if p.is_file():
            out.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(p.read_bytes()).decode()}})
    return out


def load_catalog(key: str) -> dict:
    return yaml.safe_load((CATALOG_DIR / f"{key}.yaml").read_text(encoding="utf-8"))


def save_catalog(key: str, doc: dict) -> None:
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    (CATALOG_DIR / f"{key}.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=200),
                                             encoding="utf-8")


def catalog_text(cat: dict) -> str:
    return "\n".join(f"{t['code']} {t['name']}：{t['desc']}" for t in cat["types"])


def request(parts: list[dict], schema: dict, thinking: str) -> dict:
    return {"contents": [{"parts": parts}], "generationConfig": {
        "responseMimeType": "application/json", "responseSchema": schema, "temperature": 0,
        "maxOutputTokens": 65536, "thinkingConfig": {"thinkingLevel": thinking}}}


# ─────────────────────────── 歸納題型 ───────────────────────────

TYPES_SCHEMA = {"type": "object", "properties": {"types": {"type": "array", "items": {"type": "object", "properties": {
    "code": {"type": "string"}, "name": {"type": "string"}, "desc": {"type": "string"}},
    "required": ["code", "name", "desc"]}}}, "required": ["types"]}


def discover_prompt(con: sqlite3.Connection, unit: str, n: int) -> tuple[str, dict]:
    g, sem, code = parse_unit(unit)
    qs = load_unit(con, g, sem, code)
    seen, uniq = set(), []
    for q in qs:                                       # 重複出現的題目只留一份
        k = norm(q["stem"])
        if k not in seen:
            seen.add(k)
            uniq.append(q)
    random.Random(1).shuffle(uniq)
    sample = uniq[:n]
    titles = collections.Counter(q["unit_title"] for q in qs)
    title = titles.most_common(1)[0][0]
    others = [t for t in titles if t != title]
    alias = "（其他版本稱為：" + "、".join(others) + "）" if others else ""
    half = "上下"[sem - 1]
    prompt = (f"你是國中數學老師，要替題庫建立「題型目錄」。以下是{GRADE_NAME[g]}年級{half}學期"
              f"「{code} {title}」單元{alias}的 {len(sample)} 題段考題"
              f"（從 {len(qs)} 題中抽樣）。請歸納出這個單元的題型，通常 8～15 個。\n{PRINCIPLES}\n"
              "每個題型給 code（T01 起，依課本教學順序排列）、name（12 字內）、desc（判斷標準一句話）。\n\n"
              + "\n".join(text_of(q, 220) for q in sample))
    meta = {"unit": {"grade": g, "semester": sem, "code": code, "title": title, "other_titles": others},
            "sampled": len(sample), "unit_questions": len(qs)}
    return prompt, meta


def write_catalog(key: str, meta: dict, types: list[dict], model: str) -> None:
    save_catalog(key, {**{"unit": meta["unit"]}, "status": "draft", "model": model, "sampled": meta["sampled"],
                       "unit_questions": meta["unit_questions"],
                       "types": [{"code": t["code"], "name": t["name"], "desc": t["desc"]} for t in types]})


def discover(con, units: list[str], n: int, force: bool, batch: bool) -> None:
    todo = []
    for u in units:
        key = unit_key(*parse_unit(u))
        if (CATALOG_DIR / f"{key}.yaml").exists() and not force:
            continue
        todo.append(u)
    if not todo:
        print("目錄都已存在（--force 重做）")
        return
    if not batch:
        for u in todo:
            prompt, meta = discover_prompt(con, u, n)
            out, model, usd = call_model([{"text": prompt}], MODEL, schema=TYPES_SCHEMA, fallback=[MODEL], thinking="medium")
            twd = B.Ledger.add(PHASE, usd)
            write_catalog(unit_key(*parse_unit(u)), meta, out["types"], model)
            print(f"{u}：{len(out['types'])} 個題型，US${usd:.4f}｜階段累計 NT${twd:.0f}", flush=True)
        return
    metas, reqs = {}, []
    for u in todo:
        prompt, meta = discover_prompt(con, u, n)
        key = unit_key(*parse_unit(u))
        metas[key] = meta
        reqs.append((f"discover|{key}", request([{"text": prompt}], TYPES_SCHEMA, "medium")))
    job = B.gemini_submit(PHASE, f"math_discover_{time.strftime('%m%d_%H%M%S')}", MODEL, reqs, est_usd=0.03 * len(reqs))
    job["metas"] = metas
    B.save_job(PHASE, job)
    print(f"送出 {len(reqs)} 個單元的題型歸納 → {job['name']}", flush=True)
    collect()


# ─────────────────────────── 歸類與難度 ───────────────────────────

CLASSIFY_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "id": {"type": "string"}, "type_code": {"type": "string"}, "difficulty": {"type": "integer"},
    "steps": {"type": "integer"}, "unit": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["id", "unit", "type_code", "difficulty", "steps", "reason"]}}}, "required": ["items"]}
FIELDS = ("unit", "type_code", "difficulty", "steps", "reason")


def classify_parts(cat: dict, chunk: list[dict], units: list[tuple[str, str]]) -> list[dict]:
    u = cat["unit"]
    parts: list[dict] = [{"text": (
        f"你是國中數學老師。下列題目被歸在{GRADE_NAME[u['grade']]}年級「{u['code']} {u['title']}」單元，但原本的單元歸類可能有誤。\n"
        "每題先判斷 unit：題目主要考的是本學期哪個單元（填代號）。屬於本單元時，再依題型目錄歸類"
        "（選 1 個最主要的題型；都不符合填 OTHER）；不屬於本單元時 type_code 填 OTHER。每題都要評難度。\n"
        "本學期各單元：" + "、".join(f"{c} {t}" for c, t in units) + "\n"
        + RUBRIC + "\nsteps＝解題主要步驟數；reason＝20 字內的難度理由。附圖緊接在該題文字之後。\n\n"
        f"「{u['code']}」的題型目錄：\n" + catalog_text(cat) + "\n\n題目：")}]
    for q in chunk:
        parts.append({"text": "\n" + text_of(q, 500)})
        parts += image_parts(q)
    return parts


def chunks_of(qs: list[dict], size: int) -> list[list[dict]]:
    order = qs[:]
    random.Random(100).shuffle(order)                 # 打散，避免同一份卷的題目擠在一起互相影響
    return [order[i:i + size] for i in range(0, len(order), size)]


def save_results(key: str, cat: dict, qs: list[dict], items: dict[str, dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    meta = load_results(key)["meta"]                   # 從別的單元移進來的題目也要留著它們的資料
    meta |= {q["id"]: {"type": q["type"], "answer_source": q["answer_source"], "figs": len(q["figs"]),
                      "position": round(q["number"] / q["max_no"], 3) if q["max_no"] else None} for q in qs}
    (OUT_DIR / f"{key}.json").write_text(json.dumps({"unit": cat["unit"], "results": items, "meta": meta},
                                                    ensure_ascii=False, indent=1), encoding="utf-8")


def load_results(key: str) -> dict:
    p = OUT_DIR / f"{key}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"results": {}, "meta": {}}


def classify_sync(cat: dict, qs: list[dict], size: int, workers: int,
                  units: list[tuple[str, str]]) -> tuple[dict[str, dict], float]:
    by_short = {q["short"]: q for q in qs}
    out, total = {}, 0.0

    def one(chunk):
        r, _, usd = call_model(classify_parts(cat, chunk, units), MODEL, schema=CLASSIFY_SCHEMA, fallback=[MODEL], thinking="low")
        return r["items"], usd
    with ThreadPoolExecutor(workers) as pool:
        for items, usd in pool.map(one, chunks_of(qs, size)):
            total += usd
            for it in items:
                if (q := by_short.get(it["id"])):
                    out[q["id"]] = {k: it.get(k) for k in FIELDS}
    return out, total


def classify(con, units: list[str], size: int, workers: int, batch: bool, force: bool) -> None:
    if not batch:
        for u in units:
            g, sem, code = parse_unit(u)
            key = unit_key(g, sem, code)
            if (OUT_DIR / f"{key}.json").exists() and not force:
                continue
            cat, qs = load_catalog(key), load_unit(con, g, sem, code)
            items, usd = classify_sync(cat, qs, size, workers, semester_units(con, g, sem))
            twd = B.Ledger.add(PHASE, usd)
            save_results(key, cat, qs, items)
            print(f"{u}：{len(items)}/{len(qs)} 題，US${usd:.4f}｜階段累計 NT${twd:.0f}", flush=True)
        return
    reqs = []
    for u in units:
        g, sem, code = parse_unit(u)
        key = unit_key(g, sem, code)
        if (OUT_DIR / f"{key}.json").exists() and not force:
            continue
        cat, qs = load_catalog(key), load_unit(con, g, sem, code)
        sem_units = semester_units(con, g, sem)
        for i, c in enumerate(chunks_of(qs, size)):
            reqs.append((f"classify|{key}|{i}", request(classify_parts(cat, c, sem_units), CLASSIFY_SCHEMA, "low")))
    if not reqs:
        print("都已歸類（--force 重做）")
        return
    # 一個批次檔別太大：每 1500 個請求送一批
    for s in range(0, len(reqs), 1500):
        part = reqs[s:s + 1500]
        job = B.gemini_submit(PHASE, f"math_classify_{time.strftime('%m%d_%H%M%S')}_{s // 1500}", MODEL, part,
                              est_usd=0.009 * len(part))
        print(f"送出 {len(part)} 組（每組 {size} 題）→ {job['name']}", flush=True)
    collect()


def fill(con, units: list[str], size: int, workers: int) -> None:
    """批次偶爾漏回幾題：把沒有結果的題目補跑一次。"""
    for u in units:
        g, sem, code = parse_unit(u)
        key = unit_key(g, sem, code)
        if not (OUT_DIR / f"{key}.json").exists():
            continue
        cat, qs, res = load_catalog(key), load_unit(con, g, sem, code), load_results(key)["results"]
        miss = [q for q in qs if q["id"] not in res]
        if not miss:
            continue
        items, usd = classify_sync(cat, miss, size, workers, semester_units(con, g, sem))
        res.update(items)
        save_results(key, cat, qs, res)
        twd = B.Ledger.add(PHASE, usd)
        print(f"{u}：補 {len(items)}/{len(miss)} 題，US${usd:.4f}｜階段累計 NT${twd:.0f}", flush=True)


def relocate(con, units: list[str], size: int, workers: int) -> None:
    """模型判斷不屬於原單元的題目，改用它真正所屬單元的題型目錄重新歸類。"""
    moves: dict[str, list[tuple[str, dict]]] = collections.defaultdict(list)
    for u in units:
        g, sem, code = parse_unit(u)
        key = unit_key(g, sem, code)
        res = load_results(key)["results"]
        if not res:
            continue
        valid = {c for c, _ in semester_units(con, g, sem)}
        by_id = {q["id"]: q for q in load_unit(con, g, sem, code)}
        for i, r in res.items():
            target = r.get("unit")
            if i in by_id and target != code and target in valid and not r.get("from"):
                moves[unit_key(g, sem, target)].append((key, by_id[i]))
    for tkey, lst in sorted(moves.items()):
        if not (CATALOG_DIR / f"{tkey}.yaml").exists() or not (OUT_DIR / f"{tkey}.json").exists():
            continue
        g, sem, code = parse_unit(tkey.replace("_", ":", 1))
        cat, qs, res = load_catalog(tkey), load_unit(con, g, sem, code), load_results(tkey)["results"]
        todo = [q for _, q in lst if q["id"] not in res]
        if not todo:
            continue
        items, usd = classify_sync(cat, todo, size, workers, semester_units(con, g, sem))
        src = {q["id"]: k for k, q in lst}
        for i, r in items.items():
            r["from"] = src[i]
            if r.get("unit") != code:              # 又被判到別處：留在這裡、算「歸不進」
                r["type_code"] = "OTHER"
            r["unit"] = code
        res.update(items)
        save_results(tkey, cat, qs + todo, res)
        twd = B.Ledger.add(PHASE, usd)
        print(f"{tkey}：移入 {len(items)} 題，US${usd:.4f}｜階段累計 NT${twd:.0f}", flush=True)


def in_unit(res: dict[str, dict], code: str) -> dict[str, dict]:
    """屬於這個單元的題目（含從別的單元移進來的）。"""
    return {i: r for i, r in res.items() if r.get("unit") == code}


# ─────────────────────────── 收回批次 ───────────────────────────

def parse_response(r: dict) -> dict:
    text = "".join(x.get("text", "") for x in r["candidates"][0]["content"]["parts"] if not x.get("thought"))
    return json.loads(text)


def collect() -> None:
    con = sqlite3.connect(f"file:{REPO / 'data/oleqs.db'}?mode=ro", uri=True)
    for job in B.load_jobs(PHASE):
        if job.get("collected"):
            continue
        s, res = B.wait(job, every=90, log=lambda x: print(x, flush=True))
        usd, ok, bad = 0.0, 0, 0
        grouped: dict[str, dict[int, dict]] = collections.defaultdict(dict)
        for k in job["keys"]:
            r = res.get(k) or {}
            usd += B.gemini_cost(job["model"], r.get("usageMetadata") or {})
            try:
                out = parse_response(r)
            except Exception as exc:  # noqa: BLE001
                bad += 1
                print(f"  失敗 {k}：{str(exc)[:80]}")
                continue
            ok += 1
            kind, key, *rest = k.split("|")
            if kind == "discover":
                write_catalog(key, job["metas"][key], out["types"], f"{job['model']}(batch)")
            else:
                grouped[key][int(rest[0])] = out
        for key, parts in grouped.items():
            g, sem, code = parse_unit(key.replace("_", ":", 1))
            cat, qs = load_catalog(key), load_unit(con, g, sem, code)
            by_short = {q["short"]: q for q in qs}
            items = load_results(key)["results"]
            for out in parts.values():
                for it in out["items"]:
                    if (q := by_short.get(it["id"])):
                        items[q["id"]] = {f: it.get(f) for f in FIELDS}
            save_results(key, cat, qs, items)
        twd = B.Ledger.add(PHASE, usd)
        job.update({"collected": True, "state": s, "usd": round(usd, 4)})
        job.pop("_raw", None)
        B.save_job(PHASE, job)
        print(f"收回 {job['label']}：成功 {ok}、失敗 {bad}，US${usd:.3f}｜階段累計 NT${twd:.0f}", flush=True)


# ─────────────────────────── 修整目錄 ───────────────────────────

SPLIT_SCHEMA = {"type": "object", "properties": {"types": {"type": "array", "items": {"type": "object", "properties": {
    "name": {"type": "string"}, "desc": {"type": "string"}}, "required": ["name", "desc"]}}}, "required": ["types"]}


def next_code(cat: dict) -> str:
    n = max((int(t["code"][1:]) for t in cat["types"] if re.fullmatch(r"T\d+", t["code"])), default=0) + 1
    return f"T{n:02d}"


def load_moved(con, res_all: dict[str, dict]) -> list[dict]:
    """從別的單元移進來的題目（依來源單元載入）。"""
    by_src = collections.defaultdict(set)
    for i, r in res_all.items():
        if r.get("from"):
            by_src[r["from"]].add(i)
    out = []
    for src, ids in by_src.items():
        g, sem, code = parse_unit(src.replace("_", ":", 1))
        out += [q for q in load_unit(con, g, sem, code) if q["id"] in ids]
    return out


def refine(con, units: list[str], size: int, workers: int) -> None:
    for u in units:
        g, sem, code = parse_unit(u)
        key = unit_key(g, sem, code)
        cat, qs = load_catalog(key), load_unit(con, g, sem, code)
        res_all = load_results(key)["results"]
        res = in_unit(res_all, code)
        if not res:
            continue
        by_id = {q["id"]: q for q in qs} | {q["id"]: q for q in load_moved(con, res_all)}
        n = len(res)
        cnt = collections.Counter(r["type_code"] for r in res.values())
        redo: set[str] = set()
        usd = 0.0
        notes = cat.setdefault("refined", [])
        for t in list(cat["types"]):
            members = [i for i, r in res.items() if r["type_code"] == t["code"] and i in by_id]
            if len(members) / n <= SPLIT_SHARE or len(members) < 20:
                continue
            sample = [by_id[i] for i in members][:150]
            prompt = (f"國中數學「{cat['unit']['code']} {cat['unit']['title']}」單元的題型目錄中，「{t['name']}」"
                      f"（{t['desc']}）收了 {len(members)} 題、佔全單元 {len(members) / n:.0%}，內容太雜。"
                      f"請依解題核心、情境或圖形結構把它拆成 2～4 個題型。\n{PRINCIPLES}\n"
                      f"同單元的其他題型（不要和它們重疊）：\n"
                      + "\n".join(f"{x['code']} {x['name']}" for x in cat["types"] if x["code"] != t["code"])
                      + "\n\n這個題型目前的題目：\n" + "\n".join(text_of(q, 200) for q in sample))
            out, _, c = call_model([{"text": prompt}], MODEL, schema=SPLIT_SCHEMA, fallback=[MODEL], thinking="medium")
            usd += c
            new = out["types"]
            if len(new) < 2:
                continue
            i = cat["types"].index(t)
            subs = [{"code": t["code"], "name": new[0]["name"], "desc": new[0]["desc"]}]
            for x in new[1:]:
                subs.append({"code": next_code({"types": cat["types"] + subs}), "name": x["name"], "desc": x["desc"]})
            cat["types"][i:i + 1] = subs
            notes.append(f"拆分 {t['code']} {t['name']}（{len(members)} 題）→ " + "、".join(f"{s['code']} {s['name']}" for s in subs))
            redo.update(members)
        others = [i for i, r in res.items() if r["type_code"] == "OTHER" and r.get("unit") == code and i in by_id]
        if len(others) / n > OTHER_SHARE and len(others) >= 5:
            prompt = (f"國中數學「{cat['unit']['code']} {cat['unit']['title']}」單元有 {len(others)} 題歸不進現有題型。"
                      f"請判斷需要新增哪些題型（0～3 個；零星、不成類的題目不必新增）。\n{PRINCIPLES}\n現有題型：\n"
                      + catalog_text(cat) + "\n\n歸不進的題目：\n" + "\n".join(text_of(by_id[i], 200) for i in others[:120]))
            out, _, c = call_model([{"text": prompt}], MODEL, schema=SPLIT_SCHEMA, fallback=[MODEL], thinking="medium")
            usd += c
            for x in out["types"]:
                cat["types"].append({"code": next_code(cat), "name": x["name"], "desc": x["desc"]})
                notes.append(f"新增 {cat['types'][-1]['code']} {x['name']}（從 {len(others)} 題歸不進的題目歸納）")
            if out["types"]:
                redo.update(others)
        if redo:
            save_catalog(key, cat)
            items, c = classify_sync(cat, [by_id[i] for i in redo], size, workers, semester_units(con, g, sem))
            usd += c
            for i, r in items.items():
                if res_all.get(i, {}).get("from"):
                    r["from"], r["unit"] = res_all[i]["from"], code
            res_all.update(items)
            save_results(key, cat, qs, res_all)
        twd = B.Ledger.add(PHASE, usd)
        print(f"{u}：重新歸類 {len(redo)} 題，US${usd:.4f}｜階段累計 NT${twd:.0f}", flush=True)
        for x in notes[-5:] if redo else []:
            print("   ", x)


# ─────────────────────────── 寫回題庫 ───────────────────────────

BOOK = {(7, 1): "七上", (7, 2): "七下", (8, 1): "八上", (8, 2): "八下", (9, 1): "九上", (9, 2): "九下"}


def final_labels(con, units: list[str]) -> dict[str, dict]:
    """每題最後的單元、題型、難度。移到別的單元的題目以目標單元的歸類為準。"""
    out: dict[str, dict] = {}
    for u in units:
        g, sem, code = parse_unit(u)
        key = unit_key(g, sem, code)
        for i, r in load_results(key)["results"].items():
            if not r.get("difficulty"):
                continue
            if r.get("unit") == code:
                out[i] = {"g": g, "sem": sem, "code": code, "type": r["type_code"], "d": int(r["difficulty"]),
                          "moved": bool(r.get("from"))}
            else:                                        # 判到別處但沒移成：留在原單元、算「歸不進」
                out.setdefault(i, {"g": g, "sem": sem, "code": code, "type": "OTHER", "d": int(r["difficulty"]),
                                   "moved": False})
    return out


def apply(con, units: list[str], db_path: Path, dry: bool) -> None:
    labels = final_labels(con, units)
    rw = sqlite3.connect(db_path)
    # 各版本單元名稱：(版本, 年級, 學期, 代號) → (名稱, 章)
    names = {}
    for pub, g, sem, c, t, ch in rw.execute("""select q.unit_publisher, d.grade, d.semester, q.unit_code, q.unit_title,
            q.unit_chapter, count(*) from question q join document d on d.id = q.document_id
            where d.subject = '數學' and q.unit_code is not null group by 1, 2, 3, 4, 5, 6 order by 7"""):
        names[(pub, g, sem, c)] = (t, ch)
    cur = {r[0]: r[1:] for r in rw.execute("""select q.id, q.unit_publisher, q.unit_code from question q
            join document d on d.id = q.document_id where d.subject = '數學'""")}
    moved = typed = 0
    if not dry:
        rw.execute("delete from tag where axis = 'pattern' and question_id in (select q.id from question q "
                   "join document d on d.id = q.document_id where d.subject = '數學')")
    for qid, x in labels.items():
        if qid not in cur:
            continue
        pub, old_code = cur[qid]
        if dry:
            moved += old_code != x["code"]
            typed += x["type"] != "OTHER"
            continue
        rw.execute("update question set difficulty = ? where id = ?", (x["d"], qid))
        if old_code != x["code"]:
            t, ch = names.get((pub, x["g"], x["sem"], x["code"])) or names.get(("翰林", x["g"], x["sem"], x["code"]), (None, None))
            rw.execute("update question set unit_code = ?, unit_title = ?, unit_chapter = ? where id = ?",
                       (x["code"], t, ch, qid))
            rw.execute("update tag set value = ? where question_id = ? and axis = 'textbook'",
                       (f"{BOOK[(x['g'], x['sem'])]} {x['code']} {t}", qid))
            moved += 1
        if x["type"] != "OTHER":
            rw.execute("insert into tag (question_id, axis, value, is_primary, labeled_by) values (?, 'pattern', ?, 1, 'ai')",
                       (qid, f"{x['g']}-{x['sem']}_{x['code']}:{x['type']}"))
            typed += 1
    if not dry:
        rw.commit()
    print(f"{'（試算）' if dry else ''}難度 {len(labels):,} 題｜題型 {typed:,} 題｜改單元 {moved:,} 題")


# ─────────────────────────── 報告 ───────────────────────────

def report(con, units: list[str], verbose: bool) -> None:
    for u in units:
        key = unit_key(*parse_unit(u))
        if not (CATALOG_DIR / f"{key}.yaml").exists():
            print(f"{u}：還沒有題型目錄")
            continue
        cat, data = load_catalog(key), load_results(key)
        res_all = data["results"]
        if not res_all:
            print(f"{u}：尚未歸類")
            continue
        code = cat["unit"]["code"]
        res = in_unit(res_all, code)
        moved_in = sum(1 for r in res.values() if r.get("from"))
        n = len(res)
        cnt = collections.Counter(r["type_code"] for r in res.values())
        diff = collections.Counter(r["difficulty"] for r in res.values())
        off = len(res_all) - n
        print(f"== {u} {cat['unit']['title']}：{n} 題（移入 {moved_in}、移出 {off}）｜歸不進 {cnt.get('OTHER', 0)}｜"
              f"難度 " + " ".join(f"{d}:{diff.get(d, 0)}" for d in range(1, 6)))
        if verbose:
            for t in cat["types"]:
                print(f"   {t['code']} {cnt.get(t['code'], 0):4d}  {t['name']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["discover", "classify", "fill", "relocate", "refine", "collect", "report", "apply"])
    ap.add_argument("units", nargs="*", default=["all"])
    ap.add_argument("--db", type=Path, default=REPO / "data/oleqs.db")
    ap.add_argument("--discover-n", type=int, default=300)
    ap.add_argument("--chunk", type=int, default=20)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--batch", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    units = expand(con, args.units)
    if args.action == "discover":
        discover(con, units, args.discover_n, args.force, args.batch)
    elif args.action == "classify":
        classify(con, units, args.chunk, args.workers, args.batch, args.force)
    elif args.action == "fill":
        fill(con, units, args.chunk, args.workers)
    elif args.action == "relocate":
        relocate(con, units, args.chunk, args.workers)
    elif args.action == "refine":
        refine(con, units, args.chunk, args.workers)
    elif args.action == "apply":
        apply(con, units, args.db, args.dry_run)
    elif args.action == "collect":
        collect()
    else:
        report(con, units, args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
