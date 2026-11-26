#!/usr/bin/env python3
"""自命題（ORIG_*.jsonl）→ 審題台頁面資料 web/public/original/（不進題庫）。

  python scripts/build_original_review.py <資料夾…>     # 後面的資料夾覆蓋前面（同 id 以後者為準）；REVISED_*.jsonl 補解說
"""
import collections, glob, json, re, sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "web/public/original"
NORM = {"single_choice": "single", "multiple_choice": "multiple"}


def main():
    rows = {}
    for d in sys.argv[1:]:
        for f in sorted(Path(d).rglob("ORIG_*.jsonl")):
            if "REVISED" in f.name:
                continue
            for l in f.read_text(encoding="utf-8").splitlines():
                if l.strip():
                    r = json.loads(l)
                    rows[r["id"]] = r
    for d in sys.argv[1:]:                    # 補解說
        for f in sorted(Path(d).rglob("*REVISED.jsonl")):
            for l in f.read_text(encoding="utf-8").splitlines():
                if l.strip():
                    x = json.loads(l)
                    if x["id"] in rows and x.get("explanation"):
                        rows[x["id"]]["explanation"] = x["explanation"]
    by = collections.defaultdict(list)
    for r in rows.values():
        t = NORM.get(r["type"], r["type"])
        o = r.get("options")
        if isinstance(o, dict):
            o = [{"label": k, "content": v} for k, v in o.items()]
        a = r["answer"]
        if not isinstance(a, list):
            a = re.findall(r"[A-E]", a) if t in ("single", "multiple") else [a]
        by[r["subject"]].append({
            "id": r["id"], "bk": r["book"], "u": r["unit"], "ut": r["unit_title"], "t": t, "d": r["difficulty"],
            "g": r.get("group_stem"), "s": r["stem"], "o": [[x["label"], x["content"]] for x in o or []], "a": a,
            "e": r.get("explanation") or "", "p": r.get("pattern_name"), "sr": r.get("scoring_rule")})
    OUT.mkdir(parents=True, exist_ok=True)
    idx = {}
    for s, items in by.items():
        items.sort(key=lambda x: x["id"])
        (OUT / f"{s}.json").write_text(json.dumps(items, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        idx[s] = {"n": len(items), "books": dict(collections.Counter(x["bk"] for x in items)),
                  "types": dict(collections.Counter(x["t"] for x in items))}
    (OUT / "index.json").write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    print({s: v["n"] for s, v in idx.items()}, sum(v["n"] for v in idx.values()))


if __name__ == "__main__":
    main()
