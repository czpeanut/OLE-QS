"""把高中題庫匯出成「分章節／題型」的工作批次（給執行 agent 逐題標註用）。

  python scripts/senior_label_export.py --out out/senior_label_work

每批一個 JSONL，一行一題，批次和第三輪交件相同（<科目>_<N>，附圖在同名的 ROUND3 交件包裡）。
另輸出 batches.json（每批卷數、題數、附圖所在的雲端交件包）。
"""
import argparse, json, re, sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BANK = ROOT / "data/high_school/bank"
MANIFEST = ROOT / "data/high_school/round3_manifest.json"
PACKAGES = ROOT / "data/high_school/round3_final_packages.json"
SEM = {1: "上", 2: "下"}
GRADE = {10: "高一", 11: "高二", 12: "高三"}


def book_hint(doc: dict) -> str:
    """建議的冊：有 course 用 course；沒有就是必修，寫年級學期，由 agent 對應到實際冊名。"""
    g, s = doc["grade"], doc["semester"]
    c = doc.get("course")
    if c in ("數A", "數B"):
        return f"{c} {'第三冊' if s == 1 else '第四冊'}"
    if c in ("數甲", "數乙"):
        return f"{c} {'上' if s == 1 else '下'}冊"
    if c:
        return c
    return f"必修（{GRADE[g]}{SEM[s]}學期）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    man = json.loads(MANIFEST.read_text())
    pk = json.loads(PACKAGES.read_text())["packages"]
    batch_of = {p["id"]: p["batch"] for p in man["papers"]}
    a.out.mkdir(parents=True, exist_ok=True)
    files, stats = {}, {}
    for y in sorted(BANK.glob("*.yaml")):
        d = yaml.safe_load(y.read_text())
        doc = d["document"]
        b = batch_of[doc["id"]]
        subj = doc.get("sub_subject") or doc["subject"]
        secname = {s["ord"]: s["name"] for s in doc.get("sections") or []}
        paper = {
            "paper_id": doc["id"], "subject": subj, "grade": doc["grade"], "semester": doc["semester"],
            "exam_seq": doc["exam_seq"], "course": doc.get("course"), "book_hint": book_hint(doc),
            "school": doc["school_short"], "year_roc": doc["academic_year_roc"],
            "scope_note": doc.get("scope_note"), "title": doc.get("title"),
        }
        shared = {s["key"]: s["file"] for s in d.get("shared_assets") or []}
        f = files.get(b) or files.setdefault(b, open(a.out / f"{b}.jsonl", "w", encoding="utf-8"))
        st = stats.setdefault(b, {"subject": subj, "papers": 0, "questions": 0})
        st["papers"] += 1
        for q in d["questions"]:
            figs = [x["file"] for x in q.get("assets") or []]
            if q.get("shared_asset") in shared:
                figs.append(shared[q["shared_asset"]])
            rec = {
                "id": q["id"], **paper,
                "section_name": secname.get(q["section"]), "number": q["number"], "type": q["type"],
                "group_stem": q.get("group_stem"), "stem": q["stem"],
                "options": [{"label": o["label"], "content": o.get("content"),
                             **({"figure": o["asset"]["file"]} if o.get("asset") else {})} for o in q.get("options") or []],
                "figures": figs, "answer": q.get("answer"),
                "explanation": (q.get("explanation") or "")[:400] or None,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            st["questions"] += 1
    for f in files.values():
        f.close()
    for b, st in stats.items():
        st["figures_package"] = pk[b]["title"]
        st["figures_package_sha256"] = pk[b]["sha256"]
    (a.out / "batches.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1))
    print(f"{len(stats)} 批、{sum(s['papers'] for s in stats.values())} 卷、{sum(s['questions'] for s in stats.values())} 題 → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
