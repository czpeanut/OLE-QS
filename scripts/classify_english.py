#!/usr/bin/env python3
"""英語題目依內容主題（文法、字彙、閱讀…）歸類。

英語各版本課名不同、考卷多半看不出版本，改用跨版本的主題列表
（data/curriculum/english_topics.yaml）。一份卷一次呼叫：送出主題列表與全卷題目的
精簡文字，模型逐題選一個最主要的主題。

主題列表格式：
    topics:
      - id: G23            # 代號，寫進題目標籤
        title: 過去完成式
        group: 文法        # 分組（文法、字彙、閱讀…），可省略
        grade: 9           # 第一次教到的年級與學期，可省略；
        semester: 1        #   有填時，比這份卷晚教的主題不列入候選

寫回題目（沿用其他科的欄位，網頁與統計不用改）：
    tags.chapter   {publisher: null, book, subject: 英語, code, title, chapter: 分組}
    tags.textbook  「八下 英語 過去完成式」
    tags.labeled_by: ai

已歸類過的卷自動跳過；--force 重做。

用法:
    python scripts/classify_english.py data/bank --batch --budget-twd 7000 [--limit 3]
    python scripts/classify_english.py data/bank --budget-twd 7000 --limit 2   # 即時呼叫，試跑用
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from classify_chapters import FALLBACK, MODEL, TERM, short  # noqa: E402
from generate_answers import load_env_file  # noqa: E402
from vlm_extract import call_model  # noqa: E402

PHASE = "chapters_en"
NUMBERED = re.compile(r"^\s*[(（]?\d{1,3}\s*[.．、)）]\s*")   # 題幹開頭印的題號，會和 Q 代號混淆

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "items": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {"q": {"type": "STRING"}, "topic": {"type": "STRING"}},
            "required": ["q", "topic"]}},
    },
    "required": ["items"],
}

PROMPT = """你是國中英語老師，要把一份段考卷的每一題歸到一個內容主題。

考卷：{grade}年級{term}學期第{exam}次段考，英語。{scope}

主題列表，每行開頭是主題代號：
{topics}

規則：
1. 每題選一個最主要的主題代號填在 topic：這題要答對，最關鍵的是哪個文法或能力。
2. 文法選擇題、句型改寫、翻譯依考的文法歸類；只考單字意思或拼字的歸到字彙類；
   閱讀測驗、克漏字、看圖或對話理解等依列表中最接近的類別。
3. 真的無法判斷時 topic 填空字串，不要猜。

題目（每題開頭的 Q 代號填在 q；不要用題目上印的題號）：
{questions}
"""


def load_topics(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["topics"]


def available(topics: list[dict], grade: int, sem: int) -> list[dict]:
    """這份卷時已經教過的主題（沒標年級的一律列入）。"""
    now = (grade, sem)
    return [t for t in topics if not t.get("grade") or (t["grade"], t.get("semester") or 1) <= now]


def build(path: Path, topics: list[dict], force: bool) -> tuple[str, str | None]:
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    m = d["document"]
    qs = d.get("questions") or []
    if m.get("subject") not in ("英語", "英文") or not qs:
        return "skip", None
    if not str(m.get("extractor", "")).startswith("vlm"):
        return "skip", None            # 規則式舊卷會被視覺擷取取代
    if not force and all(((q.get("tags") or {}).get("chapter") or {}).get("code") for q in qs):
        return "done", None
    shown = available(topics, m["grade"], m["semester"])
    lines = [f"{t['id']}  " + (f"{t['group']}・" if t.get("group") else "") + t["title"]
             + (f"（{'／'.join(f'{p[0]}{v}' for p, v in t['lessons'].items())}）" if t.get("lessons") else "")
             for t in shown]

    seen_groups: set[str] = set()
    qlines = []
    for i, q in enumerate(qs):
        g = (q.get("group_stem") or "").strip()
        head = ""
        if g and g not in seen_groups:
            seen_groups.add(g)
            head = f"〔題組〕{short(g, 300)}\n"
        opts = "；".join(short(o.get("content"), 30) for o in q.get("options") or [])
        stem = NUMBERED.sub("", q.get("stem") or "")
        qlines.append(f"{head}[Q{i}] {short(stem, 180)}" + (f"（{opts}）" if opts else ""))

    scope = m.get("scope_note") or ((m.get("scope") or {}).get("raw"))
    return "ok", PROMPT.format(
        grade=TERM[m["grade"]], term="上" if m["semester"] == 1 else "下", exam=m.get("exam_seq"),
        scope=f"卷上印的考試範圍：{scope}" if scope else "卷上沒有印考試範圍。",
        topics="\n".join(lines), questions="\n".join(qlines))


def apply(path: Path, topics: list[dict], out: dict, model: str) -> int:
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    m = d["document"]
    by_id = {t["id"]: t for t in topics}
    picked = {str(it.get("q") or "").strip().lstrip("[Qq").rstrip("]"): (it.get("topic") or "").strip()
              for it in out.get("items") or []}
    book = f"{TERM[m['grade']]}{'上' if m['semester'] == 1 else '下'}"
    n = 0
    for i, q in enumerate(d.get("questions") or []):
        t = by_id.get(picked.get(str(i), ""))
        tags = q.get("tags") or {}
        tags["chapter"] = {"publisher": None, "book": book, "subject": "英語",
                           "code": t["id"] if t else None, "title": t["title"] if t else None,
                           "chapter": t.get("group") if t else None}
        if t:
            n += 1
            tags["textbook"] = f"{book} 英語 {t['title']}"
        tags["labeled_by"] = "ai"
        q["tags"] = tags
    m["chapter_index"] = {"kind": "english_topics", "model": model}
    path.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return n


def run_batch(args, topics: list[dict], paths: list[Path]) -> None:
    """批次 API（半價）：送出所有待分類的卷，輪詢到收完。"""
    import batch_api as B
    queued = {k for j in B.load_jobs(PHASE) if not j.get("collected") for k in j["keys"]}
    reqs = []
    for p in paths:
        if p.stem in queued:
            continue
        status, prompt = build(p, topics, args.force)
        if status == "ok":
            reqs.append((p.stem, {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {
                "responseMimeType": "application/json", "responseSchema": SCHEMA, "temperature": 0,
                "thinkingConfig": {"thinkingLevel": "minimal"}}}))
    est = 0.002 * len(reqs)                  # 英語卷題數多，抓其他科的近兩倍
    if reqs:
        if B.Ledger.twd() + est * B.TWD_PER_USD > args.budget_twd:
            print(f"預算不足（預估 NT${est * B.TWD_PER_USD:.0f}），不送出")
            return
        job = B.gemini_submit(PHASE, f"en_{time.strftime('%m%d_%H%M%S')}", MODEL, reqs, est_usd=est)
        print(f"送出 {len(reqs)} 份卷（預估 NT${est * B.TWD_PER_USD:.0f}）→ {job['name']}", flush=True)
    for job in B.load_jobs(PHASE):
        if job.get("collected"):
            continue
        s, res = B.wait(job, every=120, log=lambda x: print(x, flush=True))
        usd, n = 0.0, 0
        for key in job["keys"]:
            r = res.get(key) or {}
            usd += B.gemini_cost(job["model"], r.get("usageMetadata") or {})
            try:
                text = "".join(x.get("text", "") for x in r["candidates"][0]["content"]["parts"])
                n += apply(args.bank / f"{key}.yaml", topics, json.loads(text), f"{job['model']}(batch)")
            except Exception as exc:  # noqa: BLE001
                print(f"  失敗 {key}：{str(exc)[:100]}")
        twd = B.Ledger.add(PHASE, usd)
        job.update({"collected": True, "state": s, "usd": round(usd, 4)})
        job.pop("_raw", None)
        B.save_job(PHASE, job)
        print(f"收回 {job['label']}：歸類 {n} 題，US${usd:.3f}｜階段累計 NT${twd:.0f}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bank", type=Path)
    ap.add_argument("--topics", type=Path, default=Path("data/curriculum/english_topics.yaml"))
    ap.add_argument("--budget-twd", type=float, required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", help="只處理檔名含這段文字的卷")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--batch", action="store_true", help="用批次 API（半價），預算算在 out/phase2_spend.json")
    args = ap.parse_args()

    load_env_file()
    topics = load_topics(args.topics)
    paths = sorted(p for p in args.bank.glob("*英*.yaml") if not args.only or args.only in p.name)
    if args.limit:
        paths = paths[:args.limit]
    if args.batch:
        run_batch(args, topics, paths)
        return 0

    import batch_api as B
    for p in paths:
        if B.Ledger.twd() >= args.budget_twd:
            print("到達預算上限，停止")
            break
        status, prompt = build(p, topics, args.force)
        if status != "ok":
            continue
        out, model, usd = call_model([{"text": prompt}], MODEL, schema=SCHEMA, fallback=FALLBACK,
                                     thinking="minimal")
        n = apply(p, topics, out, model)
        twd = B.Ledger.add(PHASE, usd)
        print(f"{p.name}：歸類 {n} 題，US${usd:.4f}｜階段累計 NT${twd:.0f}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
