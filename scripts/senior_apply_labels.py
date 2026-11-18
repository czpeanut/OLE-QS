"""把執行 agent 的高中分章節／題型標註寫回 data/high_school/bank。

  python scripts/senior_apply_labels.py <UNITS 解壓資料夾…>   # 每個資料夾內有 labels/、senior_patterns/

- 標註原檔存一份到 data/high_school/labels/<科目>/（追溯用）
- 題型目錄複製到 data/curriculum/senior_patterns/<科目>/
- 每題寫入：
    tags.unit       {code, book, chapter, title, std: true}
    tags.textbook   「<冊> <單元名>」
    tags.pattern    {key: <單元代號>:<題型代號>, name}（數學、自然四科）
    tags.labeled_by agent
    tags.label_confidence, tags.label_note（有才寫）
    difficulty      1～5（數學、自然四科）
先跑 check_senior_labels.py 確認結束碼 0 再寫回。
"""
import json, shutil, sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BANK = ROOT / "data/high_school/bank"
UNITS = ROOT / "data/curriculum/senior.yaml"
PAT_OUT = ROOT / "data/curriculum/senior_patterns"
LAB_OUT = ROOT / "data/high_school/labels"


def dump(d: dict, path: Path) -> None:
    path.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False, width=10_000), encoding="utf-8")


def main() -> int:
    dirs = [Path(p) for p in sys.argv[1:]]
    if not dirs:
        sys.exit(__doc__)
    unit = {}
    for subj, books in yaml.safe_load(UNITS.read_text())["subjects"].items():
        for b in books:
            for u in b["units"]:
                unit[str(u["code"])] = {"code": str(u["code"]), "book": b["book"], "chapter": u.get("chapter_title"),
                                        "title": u["title"], "std": True}
    pname, labels = {}, {}
    for d in dirs:
        for pf in d.glob("senior_patterns/*/*.yaml"):
            subj = pf.parent.name
            (PAT_OUT / subj).mkdir(parents=True, exist_ok=True)
            shutil.copy(pf, PAT_OUT / subj / pf.name)
            cat = yaml.safe_load(pf.read_text())
            for t in cat.get("types") or []:
                pname[(str(cat["unit"]["code"]), t["code"])] = t["name"]
        for lf in d.glob("labels/*.jsonl"):
            subj = d.name
            (LAB_OUT / subj).mkdir(parents=True, exist_ok=True)
            shutil.copy(lf, LAB_OUT / subj / lf.name)
            for line in lf.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    labels[r["id"]] = r
    n = changed = 0
    for y in sorted(BANK.glob("*.yaml")):
        d = yaml.safe_load(y.read_text())
        dirty = False
        for q in d["questions"]:
            r = labels.get(q["id"])
            if not r:
                continue
            n += 1
            tags = {k: v for k, v in (q.get("tags") or {}).items()
                    if k not in ("unit", "textbook", "pattern", "labeled_by", "label_confidence", "label_note")}
            u = unit.get(str(r.get("unit")))
            if u:
                tags["unit"] = dict(u)
                tags["textbook"] = f"{u['book']} {u['title']}"
            p = r.get("pattern")
            if u and p and p != "OTHER":
                tags["pattern"] = {"key": f"{u['code']}:{p}", "name": pname.get((u["code"], p))}
            tags["labeled_by"] = "agent"
            if r.get("confidence"):
                tags["label_confidence"] = r["confidence"]
            if r.get("note"):
                tags["label_note"] = r["note"]
            if q.get("tags") != tags:
                q["tags"] = tags
                dirty = True
            if r.get("difficulty") in (1, 2, 3, 4, 5) and q.get("difficulty") != r["difficulty"]:
                q["difficulty"] = r["difficulty"]
                dirty = True
        if dirty:
            dump(d, y)
            changed += 1
    print(f"標註 {len(labels):,} 題，對到題庫 {n:,} 題，改寫 {changed:,} 份卷")
    return 0 if n == len(labels) else 1


if __name__ == "__main__":
    sys.exit(main())
