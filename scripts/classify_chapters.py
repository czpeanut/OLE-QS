#!/usr/bin/env python3
"""依教科書章節表（data/curriculum/junior.yaml）把題目歸到章節。

一份卷一次呼叫：送出該冊三個版本（翰林、康軒、南一）的章節列表、卷上印的考試範圍，
以及全卷題目的精簡文字，模型先判斷這份卷用的是哪個版本，再逐題選出所屬的節
（國文是課）。卷上有印版本時只給那個版本的列表。

寫回題目：
    tags.chapter   {publisher, book, subject, code, title, chapter}
    tags.textbook  「七上 1-1 正數與負數」這類可讀標籤（網頁與匯入沿用這欄）
    tags.labeled_by: ai

英語另由 classify_english.py 依內容主題歸類。
已歸類過的卷自動跳過；--force 重做。

用法:
    python scripts/classify_chapters.py data/bank --ledger out/chapter_spend.json \\
        --budget-twd 250 [--limit 3] [--workers 8]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_answers import load_env_file  # noqa: E402
from vlm_extract import TWD_PER_USD, Usage, call_model  # noqa: E402

MODEL = "gemini-3.1-flash-lite"
FALLBACK = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]
PUB_LETTER = {"翰林": "H", "康軒": "K", "南一": "N"}
SUB_LETTER = {"": "", "生物": "b", "理化": "p", "地科": "e", "歷史": "h", "地理": "g", "公民": "c"}
TERM = {7: "七", 8: "八", 9: "九"}

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "publisher": {"type": "STRING", "enum": ["翰林", "康軒", "南一", "不確定"]},
        "items": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {"i": {"type": "INTEGER"}, "unit": {"type": "STRING"}},
            "required": ["i", "unit"]}},
    },
    "required": ["publisher", "items"],
}

PROMPT = """你是國中教師，要把一份段考卷的每一題歸到教科書的章節。

考卷：{grade}年級{term}學期第{exam}次段考，{subject}。{scope}
一學期通常三次段考，依序涵蓋課本前、中、後段；第 {exam} 次段考的題目多半落在對應的那一段（也可能含前面範圍）。

下面是這一冊各版本的單元列表，每行開頭是單元代號：
{units}

步驟：
1. 先依考試範圍與題目內容判斷這份卷用哪個版本（publisher），看不出來填「不確定」。
2. 逐題選出最符合的單元代號，填在 unit。版本確定時只用該版本的代號；不確定時選任一版本中最符合的。
3. 題目與任何單元都無關（例如純國字注音但看不出出自哪一課）時 unit 填空字串。不要猜測。

題目（i 是題目序號）：
{questions}
"""


def load_units(curr: dict, grade: int, sem: int, subject: str, sub: str | None) -> list[dict]:
    """回傳這份卷可用的單元（含代號）。社會合科卷給三科，國三自然給理化加地科。"""
    book = ((curr.get(grade) or {}).get(sem) or {}).get(subject) or {}
    if subject == "社會":
        subs = [sub] if sub else ["歷史", "地理", "公民"]
    elif subject == "自然":
        subs = [s for s in ("生物", "理化", "地科") if s in book]
    else:
        subs = [""]
    out = []
    for s in subs:
        for pub, units in (book.get(s) or {}).items():
            for u in units:
                code = f"L{u['order']:02d}" if "lesson" in u else u["section"]
                out.append({"id": f"{PUB_LETTER[pub]}{SUB_LETTER[s]}{code}", "publisher": pub,
                            "subject": s or subject, "code": u.get("lesson") or code,
                            "title": u["title"],
                            "chapter": (f"第{u['chapter']}章 {u['chapter_title']}"
                                        if u.get("chapter") else None)})
    return out


# 第 k 次段考最遠考到課本的哪裡（依單元順序的比例）。模型少了這個限制，
# 會把第一次段考的題目歸到期末才教的章節。
EXAM_REACH = {1: 0.55, 2: 0.85, 3: 1.0}
# 學期的最後一次段考涵蓋全冊。國三下學期因為會考只有兩次段考，
# 第 2 次就是期末考；照 EXAM_REACH 會把最後幾節（例如電磁感應）排除在候選外。
LAST_EXAM = {(9, 2): 2}


def within_exam(units: list[dict], exam: int | None, grade: int = 0, sem: int = 0) -> list[dict]:
    last = LAST_EXAM.get((grade, sem), 3)
    reach = 1.0 if (exam or last) >= last else EXAM_REACH.get(exam, 1.0)
    out = []
    for key in dict.fromkeys((u["publisher"], u["subject"]) for u in units):
        book = [u for u in units if (u["publisher"], u["subject"]) == key]
        out += book[:max(1, math.ceil(len(book) * reach))]
    return out


def publisher_of(scope: dict | None) -> str | None:
    text = json.dumps(scope or {}, ensure_ascii=False)
    for pub, short in (("翰林", "翰"), ("康軒", "康"), ("南一", "南")):
        if pub in text or re.search(rf"{short}(版|\b|林|軒|一)", text):
            return pub
    return None


def short(s: str | None, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def build(path: Path, curr: dict, force: bool) -> tuple[str, str | None]:
    """回傳 (狀態, 提示詞)。狀態不是 ok 時提示詞為 None。"""
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    m = d["document"]
    qs = d.get("questions") or []
    if m.get("subject") in ("英語", "英文") or not qs:
        return "skip", None
    if not str(m.get("extractor", "")).startswith("vlm"):
        return "skip", None            # 規則式舊卷會被視覺擷取取代，同時寫檔會互相覆蓋
    if not force and all((q.get("tags") or {}).get("chapter") for q in qs):
        return "done", None
    units, pub = candidates(curr, m)
    if not units:
        return "nounits", None
    shown = [u for u in units if u["publisher"] == pub] if pub else units
    shown = within_exam(shown, m.get("exam_seq"), m["grade"], m["semester"])
    lines = [f"{u['id']}  {u['publisher']}・{u['subject']}・"
             + (f"{u['chapter']}・" if u["chapter"] else "") + f"{u['code']} {u['title']}"
             for u in shown]

    seen_groups: set[str] = set()
    qlines = []
    for i, q in enumerate(qs):
        g = (q.get("group_stem") or "").strip()
        head = ""
        if g and g not in seen_groups:
            seen_groups.add(g)
            head = f"〔題組〕{short(g, 220)}\n"
        opts = "；".join(short(o.get("content"), 30) for o in q.get("options") or [])
        qlines.append(f"{head}[{i}] {short(q.get('stem'), 160)}" + (f"（{opts}）" if opts else ""))

    scope = m.get("scope_note") or ((m.get("scope") or {}).get("raw"))
    return "ok", PROMPT.format(
        grade=TERM[m["grade"]], term="上" if m["semester"] == 1 else "下", exam=m.get("exam_seq"),
        subject=m["subject"] + (f"（{m['sub_subject']}）" if m.get("sub_subject") else ""),
        scope=f"卷上印的考試範圍：{scope}" + (f"（{pub}版）" if pub else "") if scope else "卷上沒有印考試範圍。",
        units="\n".join(lines), questions="\n".join(qlines))


def candidates(curr: dict, m: dict) -> tuple[list[dict], str | None]:
    units = load_units(curr, m["grade"], m["semester"], m["subject"], m.get("sub_subject"))
    pub = publisher_of(m.get("scope")) or publisher_of({"s": m.get("scope_note")})
    return units, pub


def apply(path: Path, curr: dict, out: dict, model: str) -> int:
    """把模型的選擇寫回題目標籤（重新讀檔，避免覆蓋其他步驟的修改）。"""
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    m = d["document"]
    qs = d.get("questions") or []
    units, pub = candidates(curr, m)
    by_id = {u["id"]: u for u in units}
    picked = {it.get("i"): (it.get("unit") or "").strip() for it in out.get("items") or []}
    book = f"{TERM[m['grade']]}{'上' if m['semester'] == 1 else '下'}"
    n = 0
    for i, q in enumerate(qs):
        u = by_id.get(picked.get(i, ""))
        tags = q.get("tags") or {}
        if u:
            n += 1
            sub = u["subject"] if u["subject"] != m["subject"] else ""
            tags["chapter"] = {"publisher": u["publisher"], "book": book, "subject": u["subject"],
                               "code": u["code"], "title": u["title"], "chapter": u["chapter"]}
            tags["textbook"] = f"{book}{sub} {u['code']} {u['title']}"
        else:
            tags["chapter"] = {"publisher": None, "book": book, "subject": m["subject"],
                               "code": None, "title": None, "chapter": None}
            tags.setdefault("textbook", f"{book} 第{m.get('exam_seq')}次段考範圍")
        tags["labeled_by"] = "ai"
        q["tags"] = tags
    m["chapter_index"] = {"publisher": out.get("publisher"), "model": model,
                          "publisher_from_scope": pub}
    path.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return n


def classify(path: Path, curr: dict, force: bool) -> tuple[str, int, float]:
    status, prompt = build(path, curr, force)
    if status != "ok":
        return status, 0, 0.0
    out, model, cost = call_model([{"text": prompt}], MODEL, schema=SCHEMA, fallback=FALLBACK,
                                  thinking="minimal")   # 思考 tokens 以輸出價計，實測佔一半以上費用
    return "ok", apply(path, curr, out, model), cost


def run_batch(args, curr: dict, paths: list[Path]) -> None:
    """批次 API（半價）：送出所有待分類的卷，輪詢到收完。"""
    import batch_api as B
    phase = "chapters"
    queued = {k for j in B.load_jobs(phase) if not j.get("collected") for k in j["keys"]}
    reqs = []
    for p in paths:
        if p.stem in queued:
            continue
        status, prompt = build(p, curr, args.force)
        if status == "ok":
            reqs.append((p.stem, {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {
                "responseMimeType": "application/json", "responseSchema": SCHEMA, "temperature": 0,
                "thinkingConfig": {"thinkingLevel": "minimal"}}}))
    est = 0.0012 * len(reqs)                 # 實測一般價每份 US$0.0024
    if reqs:
        if B.Ledger.twd() + est * B.TWD_PER_USD > args.budget_twd:
            print(f"預算不足（預估 NT${est * B.TWD_PER_USD:.0f}），不送出")
            return
        job = B.gemini_submit(phase, f"chapters_{time.strftime('%m%d_%H%M%S')}", MODEL, reqs, est_usd=est)
        print(f"送出 {len(reqs)} 份卷 → {job['name']}", flush=True)
    for job in B.load_jobs(phase):
        if job.get("collected"):
            continue
        s, res = B.wait(job, every=120, log=lambda x: print(x, flush=True))
        usd, n = 0.0, 0
        for key in job["keys"]:
            r = res.get(key) or {}
            usd += B.gemini_cost(job["model"], r.get("usageMetadata") or {})
            try:
                text = "".join(x.get("text", "") for x in r["candidates"][0]["content"]["parts"])
                n += apply(args.bank / f"{key}.yaml", curr, json.loads(text), f"{job['model']}(batch)")
            except Exception as exc:  # noqa: BLE001
                print(f"  失敗 {key}：{str(exc)[:100]}")
        twd = B.Ledger.add(phase, usd)
        job.update({"collected": True, "state": s, "usd": round(usd, 4)})
        job.pop("_raw", None)
        B.save_job(phase, job)
        print(f"收回 {job['label']}：歸類 {n} 題，US${usd:.3f}｜階段累計 NT${twd:.0f}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bank", type=Path)
    ap.add_argument("--curriculum", type=Path, default=Path("data/curriculum/junior.yaml"))
    ap.add_argument("--ledger", type=Path, default=Path("out/chapter_spend.json"))
    ap.add_argument("--budget-twd", type=float, required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", help="只處理檔名含這段文字的卷")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--batch", action="store_true", help="用批次 API（半價），預算算在 out/phase2_spend.json")
    args = ap.parse_args()

    load_env_file()
    curr = yaml.safe_load(args.curriculum.read_text(encoding="utf-8"))["grades"]
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    Usage.load(args.ledger)
    paths = sorted(p for p in args.bank.glob("*.yaml") if not args.only or args.only in p.name)
    if args.limit:
        paths = paths[:args.limit]
    if args.batch:
        run_batch(args, curr, paths)
        return 0
    print(f"{len(paths)} 份卷；先前費用 NT${Usage.twd():.1f}，上限 NT${args.budget_twd:.0f}", flush=True)

    stats = {"ok": 0, "done": 0, "skip": 0, "nounits": 0, "fail": 0}
    tagged = 0
    lock = threading.Lock()
    stop = threading.Event()

    def job(p: Path):
        if stop.is_set():
            return p, "budget", 0, 0.0
        try:
            return (p, *classify(p, curr, args.force))
        except Exception as exc:  # noqa: BLE001
            return p, f"fail: {exc}", 0, 0.0

    with ThreadPoolExecutor(args.workers) as pool:
        futs = [pool.submit(job, p) for p in paths]
        for k, f in enumerate(as_completed(futs), 1):
            p, status, n, cost = f.result()
            with lock:
                key = status if status in stats else ("fail" if status.startswith("fail") else None)
                if key:
                    stats[key] += 1
                tagged += n
                if Usage.twd() + 0.5 * args.workers >= args.budget_twd and not stop.is_set():
                    stop.set()
                    print(f"接近預算上限 NT${args.budget_twd:.0f}，停止送出新的卷", flush=True)
                if status == "ok" or status.startswith("fail"):
                    print(f"[{k}/{len(paths)}] {p.name} {status} {n} 題 ${cost:.4f}"
                          f"｜累計 NT${Usage.twd():.1f}", flush=True)
    print(f"完成：{stats}；歸類 {tagged} 題；累計費用 US${Usage.usd:.3f}（NT${Usage.twd():.1f}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
