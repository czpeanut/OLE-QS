#!/usr/bin/env python3
"""英語「綜合類」題目（字彙、片語、閱讀、對話、聽力、其他文法）改依原卷段考次分類。

歸到綜合類的題目改成「段考範圍」的 E1～E3（第1～3次段考），冊別沿用原卷的年級、學期；
文法主題的題目不動。同時改題庫 YAML（tags.chapter、tags.textbook）、Supabase（question 的單元欄位、
textbook 標籤），並重算 web/lib/data/counts.json 裡英語的單元題數。

    python scripts/english_exam_units.py            # 只列出會改多少題
    python scripts/english_exam_units.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.quality import evaluate  # noqa: E402
from batch_api import _env  # noqa: E402
from classify_english import EXAM_GROUP, load_topics  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BANK = REPO / "data" / "bank"
COUNTS = REPO / "web" / "lib" / "data" / "counts.json"
TERM = {7: "七", 8: "八", 9: "九"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    topics = load_topics(REPO / "data/curriculum/english_topics.yaml")
    misc = {t["id"] for t in topics if t.get("group") == EXAM_GROUP}
    exams = {t["exam"]: t for t in topics if t.get("exam")}

    changes: list[tuple[str, dict]] = []            # (題目 id, 新的單元欄位)
    counts: Counter = Counter()                     # (年級, 學期, 單元代號) → 收錄題數
    for p in sorted(BANK.glob("*_英語_*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        m = d["document"]
        book = f"{TERM[m['grade']]}{'上' if m['semester'] == 1 else '下'}"
        touched = False
        for q in d.get("questions") or []:
            ch = (q.get("tags") or {}).get("chapter") or {}
            if ch.get("code") in misc and m.get("exam_seq") in exams:
                t = exams[m["exam_seq"]]
                q["tags"]["chapter"] = {"publisher": None, "book": book, "subject": "英語", "code": t["id"],
                                        "title": t["title"], "chapter": t["group"], "from": ch.get("code")}
                q["tags"]["textbook"] = f"{book} 英語 {t['title']}"
                changes.append((q["id"], {"unit_publisher": None, "unit_subject": "英語", "unit_code": t["id"],
                                          "unit_title": t["title"], "unit_chapter": t["group"]}, q["tags"]["textbook"]))
                touched = True
            code = ((q.get("tags") or {}).get("chapter") or {}).get("code")
            if code and evaluate(q, d)[0]:
                counts[(m["grade"], m["semester"], code)] += 1
        if touched and args.apply:
            p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"改分到段考範圍：{len(changes):,} 題")
    if not args.apply:
        return 0

    # 網站的單元題數：英語整段換掉（其他科不動）
    c = json.loads(COUNTS.read_text(encoding="utf-8"))
    c["units"] = [r for r in c["units"] if r[0] != "英語"] + [
        ["英語", g, s, "英語", code, "", n] for (g, s, code), n in sorted(counts.items())]
    COUNTS.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")

    url, key = _env("SUPABASE_URL").rstrip("/"), _env("SUPABASE_SERVICE_ROLE_KEY")
    H = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
         "Prefer": "return=minimal"}
    s = requests.Session()

    def send(item):
        qid, unit, textbook = item
        for attempt in range(6):
            try:
                s.patch(f"{url}/rest/v1/question", params={"id": f"eq.{qid}"}, headers=H, json=unit,
                        timeout=60).raise_for_status()
                s.patch(f"{url}/rest/v1/tag", params={"question_id": f"eq.{qid}", "axis": "eq.textbook"},
                        headers=H, json={"value": textbook}, timeout=60).raise_for_status()
                return
            except requests.RequestException:
                if attempt == 5:
                    raise
                time.sleep(2 ** attempt)

    with ThreadPoolExecutor(args.workers) as pool:
        for i, _ in enumerate(pool.map(send, changes), start=1):
            if i % 2000 == 0:
                print(f"  Supabase {i:,}/{len(changes):,}", flush=True)
    print(f"已寫回 {len(changes):,} 題（YAML、Supabase）；英語單元題數 {len(counts)} 筆")
    return 0


if __name__ == "__main__":
    sys.exit(main())
