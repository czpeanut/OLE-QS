#!/usr/bin/env python3
"""英語主題表新增一個主題後，只把該歸到新主題的題目改過去，其他題目的歸類不動。

做法：挑出可能考這個主題的卷（題目文字符合 --pattern），用 classify_english.py 同一套提示詞
與新的主題列表重新請模型歸類整份卷；只有模型改判成新主題的題目才更新。
同時改題庫 YAML（tags.chapter、tags.textbook）、data/oleqs.db 與 Supabase。

    # 先試算：列出會改哪些題，模型輸出存在 out/retopic/<主題>/
    python scripts/retopic_english.py --topic B5-12 --budget-twd 8200
    python scripts/retopic_english.py --topic B5-12 --budget-twd 8200 --apply

預設的 --pattern 抓附加問句（..., isn't it? / don't you?）。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batch_api import Ledger, _env  # noqa: E402
from classify_chapters import FALLBACK, MODEL, TERM  # noqa: E402
from classify_english import PHASE, SCHEMA, build, load_topics  # noqa: E402
from generate_answers import load_env_file  # noqa: E402
from vlm_extract import call_model  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BANK = REPO / "data" / "bank"
DB = REPO / "data" / "oleqs.db"
AUX = r"(?:is|are|was|were|do|does|did|will|would|can|could|have|has|had|should|shall|must|may)"
TAG_QUESTION = (rf",\s*(?:{AUX}n['’]t|{AUX})\s+(?:it|he|she|they|you|we|I|there|that|this)\s*\?"
                r"|,\s*_{2,}\s*\?|附加問句|tag question")


def text_of(q: dict) -> str:
    return " ".join([q.get("stem") or "", *[o.get("content") or "" for o in q.get("options") or []]])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--pattern", default=TAG_QUESTION, help="挑候選卷用的正規式")
    ap.add_argument("--topics", type=Path, default=REPO / "data/curriculum/english_topics.yaml")
    ap.add_argument("--budget-twd", type=float, required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    load_env_file()
    topics = load_topics(args.topics)
    t = next(x for x in topics if x["id"] == args.topic)
    since = (t.get("grade") or 0, t.get("semester") or 1)
    pat = re.compile(args.pattern, re.I)
    cache = REPO / "out" / "retopic" / args.topic
    cache.mkdir(parents=True, exist_ok=True)

    changes = []
    for p in sorted(BANK.glob("*英*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        m = d["document"]
        if (m.get("grade"), m.get("semester")) < since or not str(m.get("extractor", "")).startswith("vlm"):
            continue
        qs = d.get("questions") or []
        if not any(pat.search(text_of(q)) for q in qs):
            continue
        out_f = cache / f"{p.stem}.json"
        if out_f.is_file():
            out = json.loads(out_f.read_text(encoding="utf-8"))
        else:
            if Ledger.twd() >= args.budget_twd:
                print("到達預算上限，停止")
                break
            status, prompt = build(p, topics, True)
            if status != "ok":
                continue
            out, model, usd = call_model([{"text": prompt}], MODEL, schema=SCHEMA, fallback=FALLBACK,
                                         thinking="minimal")
            Ledger.add(PHASE, usd)
            out_f.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        picked = {str(it.get("q") or "").strip().lstrip("[Qq").rstrip("]"): (it.get("topic") or "").strip()
                  for it in out.get("items") or []}
        book = f"{TERM[m['grade']]}{'上' if m['semester'] == 1 else '下'}"
        for i, q in enumerate(qs):
            old = ((q.get("tags") or {}).get("chapter") or {}).get("code")
            if picked.get(str(i)) == args.topic and old != args.topic:
                changes.append((p, q["id"], old, book, (q.get("stem") or "")[:70]))

    print(f"改判成 {args.topic} 的題目：{len(changes)} 題；累計 NT${Ledger.twd():.0f}")
    for _, qid, old, _, stem in changes:
        print(f"  {qid}  {old} → {args.topic}｜{stem}")
    if not args.apply or not changes:
        return 0

    title, group = t["title"], t.get("group")
    by_path: dict[Path, list] = {}
    for c in changes:
        by_path.setdefault(c[0], []).append(c)
    con = sqlite3.connect(DB)
    url, key = _env("SUPABASE_URL").rstrip("/"), _env("SUPABASE_SERVICE_ROLE_KEY")
    H = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Prefer": "return=minimal"}
    s = requests.Session()
    for p, cs in by_path.items():
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        want = {c[1]: c[3] for c in cs}
        for q in d.get("questions") or []:
            if q["id"] not in want:
                continue
            book = want[q["id"]]
            tags = q.get("tags") or {}
            tags["chapter"] = {"publisher": None, "book": book, "subject": "英語", "code": args.topic,
                               "title": title, "chapter": group}
            tags["textbook"] = f"{book} 英語 {title}"
            tags["labeled_by"] = "ai"
            q["tags"] = tags
            unit = {"unit_publisher": None, "unit_subject": "英語", "unit_code": args.topic,
                    "unit_title": title, "unit_chapter": group}
            con.execute("update question set unit_publisher=?, unit_subject=?, unit_code=?, unit_title=?, "
                        "unit_chapter=? where id=?", (*unit.values(), q["id"]))
            con.execute("update tag set value=? where question_id=? and axis='textbook'", (tags["textbook"], q["id"]))
            s.patch(f"{url}/rest/v1/question", params={"id": f"eq.{q['id']}"}, headers=H, json=unit,
                    timeout=60).raise_for_status()
            s.patch(f"{url}/rest/v1/tag", params={"question_id": f"eq.{q['id']}", "axis": "eq.textbook"},
                    headers=H, json={"value": tags["textbook"]}, timeout=60).raise_for_status()
        p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    con.commit()
    print(f"已寫回 {len(changes)} 題（YAML、oleqs.db、Supabase）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
