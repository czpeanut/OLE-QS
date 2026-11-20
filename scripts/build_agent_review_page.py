#!/usr/bin/env python3
"""審題台「agent 審題結果」頁面的資料：統計＋各類問題題目清單 → web/public/agent-review/。

  python scripts/build_agent_review_page.py <審題資料夾…> --db-cache db.json --plan out/junior_plan.json
"""
import argparse, collections, concurrent.futures as cf, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from junior_review_tool import get
from junior_apply_review import read_reviews, L

OUT = Path(__file__).resolve().parent.parent / "web/public/agent-review"
CAT = {"text": "題目文字有問題", "fixed": "答案被訂正", "risky": "風險題（保留原答案）", "unsure": "agent 不確定"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("reviews", nargs="+", type=Path)
    ap.add_argument("--db-cache", type=Path, required=True)
    ap.add_argument("--plan", type=Path, required=True)
    a = ap.parse_args()
    R, src = read_reviews(a.reviews)
    D = json.loads(a.db_cache.read_text(encoding="utf-8"))
    plan = json.loads(a.plan.read_text(encoding="utf-8"))
    P = plan["plan"]
    st = plan["stats"]
    # 單元：補缺（原本沒有單元或只有段考範圍 E1-E3）vs 改錯
    un = collections.defaultdict(collections.Counter)
    for i, r in R.items():
        d = D.get(i)
        if not d or r["unit"] != "fix" or "unit_code" not in P.get(i, {}):
            continue
        s = d["document"]["subject"]
        k = "補缺" if (not d["unit_code"] or re.match(r"^E[123]$", str(d["unit_code"]))) else "改錯"
        un[s][k] += 1
        un["全部"][k] += 1
    dif = collections.defaultdict(collections.Counter)
    for i, r in R.items():
        if i in D:
            dif[D[i]["document"]["subject"]][r.get("difficulty")] += 1
            dif["全部"][r.get("difficulty")] += 1
    # 清單
    cats = collections.defaultdict(list)
    for i, r in R.items():
        d = D.get(i)
        if not d:
            continue
        pi = P.get(i, {})
        if r["text"] == "issue":
            cats["text"].append(i)
        elif r["answer"] == "fix" and d["answer"] and pi.get("answer_status") == "disputed":
            cats["risky" if pi.get("answer") is None else "fixed"].append(i)
        elif r["answer"] == "unsure":
            cats["unsure"].append(i)
    need = sorted({i for v in cats.values() for i in v})

    def chunk(c):
        return get("/rest/v1/question", {"select": "id,stem_md,group_stem,option(label,content_md)", "id": "in.(" + ",".join(c) + ")"})
    Q = {}
    with cf.ThreadPoolExecutor(8) as ex:
        for res in ex.map(chunk, [need[k:k + 100] for k in range(0, len(need), 100)]):
            for q in res:
                Q[q["id"]] = q
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("items_*.json"):
        old.unlink()
    meta = {}
    for c, ids in cats.items():
        rows = []
        for i in sorted(ids):
            r, d, q = R[i], D[i], Q.get(i, {})
            doc = d["document"]
            rows.append({
                "id": i, "s": doc["subject"] + (f"·{doc['sub_subject']}" if doc.get("sub_subject") else ""),
                "g": f"{doc['grade']}-{doc['semester']}", "b": src[i],
                "stem": ((q.get("group_stem") or "")[:200] + (" ｜ " if q.get("group_stem") else "") + (q.get("stem_md") or ""))[:600],
                "opts": [[o["label"], (o.get("content_md") or "")[:80]] for o in sorted(q.get("option") or [], key=lambda o: o["label"])],
                "old": L(d["answer"]), "new": L(r.get("answer_fix")),
                "why": (r.get("text_issue") or r.get("answer_reason") or r.get("note") or "")[:400],
            })
        (OUT / f"items_{c}.json").write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        meta[c] = {"name": CAT[c], "n": len(rows)}
    subjects = ["全部", "數學", "國文", "英語", "社會", "自然"]
    (OUT / "stats.json").write_text(json.dumps({
        "subjects": subjects, "stats": {s: dict(st[s]) for s in subjects}, "unit": {s: dict(un[s]) for s in subjects},
        "difficulty": {s: {str(k): v for k, v in dif[s].items()} for s in subjects}, "cats": meta,
        "rows_written": len(P)}, ensure_ascii=False), encoding="utf-8")
    print({c: m["n"] for c, m in meta.items()}, "→", OUT)


if __name__ == "__main__":
    main()
