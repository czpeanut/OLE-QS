"""檢查高中分章節／題型標註的交件格式。

  python scripts/check_senior_labels.py --work out/senior_label_work \
      --units data/curriculum/senior.yaml --patterns data/curriculum/senior_patterns \
      --labels out/senior_labels/數學_1.jsonl [更多檔案…]

逐檔檢查：每題剛好一行、單元代號存在、題型代號存在、難度 1～5、null 一定要有原因、
題組子題分到不同單元要提醒。錯誤（✗）要全部修掉，結束碼才是 0；提醒（△）照實寫進報告即可。
"""
import argparse, collections, json, sys
from pathlib import Path

import yaml

PATTERN_SUBJECTS = {"數學", "物理", "化學", "生物", "地科"}
CONF = {"high", "medium", "low"}


def load_units(path: Path) -> dict:
    """senior.yaml → {科目: {單元代號: 冊名}}"""
    d = yaml.safe_load(path.read_text())
    out = {}
    for subj, books in (d.get("subjects") or {}).items():
        for book in books:
            for u in book.get("units") or []:
                out.setdefault(subj, {})[str(u["code"])] = book["book"]
    return out


def load_patterns(root: Path) -> dict:
    """senior_patterns/<科目>/<單元代號>.yaml → {(科目, 單元代號): {題型代號…}}"""
    out = {}
    for f in root.glob("*/*.yaml"):
        d = yaml.safe_load(f.read_text())
        out[(f.parent.name, str(d["unit"]["code"]))] = {t["code"] for t in d.get("types") or []}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--units", type=Path, required=True)
    ap.add_argument("--patterns", type=Path, required=True)
    ap.add_argument("--labels", type=Path, nargs="+", required=True)
    a = ap.parse_args()
    units = load_units(a.units)
    pats = load_patterns(a.patterns) if a.patterns.exists() else {}
    errors = 0
    for lf in a.labels:
        batch = lf.stem
        src = a.work / f"{batch}.jsonl"
        if not src.exists():
            print(f"✗ {lf.name}：找不到對應的工作檔 {src.name}（檔名要和批次相同）"); errors += 1; continue
        qs = {}
        for line in src.read_text(encoding="utf-8").splitlines():
            r = json.loads(line); qs[r["id"]] = r
        seen = collections.Counter()
        groups = collections.defaultdict(set)
        nulls = other = 0
        e0 = errors
        for n, line in enumerate(lf.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                print(f"✗ {lf.name}:{n} 不是 JSON"); errors += 1; continue
            qid = r.get("id")
            if qid not in qs:
                print(f"✗ {lf.name}:{n} id 不在這一批：{qid}"); errors += 1; continue
            seen[qid] += 1
            q = qs[qid]
            subj = q["subject"]
            u = r.get("unit")
            if u is None:
                nulls += 1
                if not r.get("note"):
                    print(f"✗ {qid}：unit 是 null，note 要寫原因"); errors += 1
            elif str(u) not in units.get(subj, {}):
                print(f"✗ {qid}：單元代號 {u} 不在 senior.yaml 的{subj}"); errors += 1
            if r.get("confidence") not in CONF:
                print(f"✗ {qid}：confidence 要是 high／medium／low"); errors += 1
            if subj in PATTERN_SUBJECTS and u is not None:
                p = r.get("pattern")
                if p == "OTHER":
                    other += 1
                elif p not in pats.get((subj, str(u)), set()):
                    print(f"✗ {qid}：題型 {p} 不在 senior_patterns/{subj}/{u}.yaml"); errors += 1
                if r.get("difficulty") not in (1, 2, 3, 4, 5):
                    print(f"✗ {qid}：difficulty 要是 1～5 的整數"); errors += 1
            if q.get("group_stem") and u is not None:
                groups[(q["paper_id"], q["group_stem"][:60])].add(str(u))
        for qid in qs:
            if seen[qid] == 0:
                print(f"✗ {qid}：漏標"); errors += 1
            elif seen[qid] > 1:
                print(f"✗ {qid}：標了 {seen[qid]} 次"); errors += 1
        for (pid, _), us in groups.items():
            if len(us) > 1:
                print(f"△ {pid}：同一題組分到不同單元 {sorted(us)}，確認是否正確")
        n = len(qs)
        print(f"{lf.name}：{n} 題，單元 null {nulls}（{nulls / max(n, 1):.1%}）、題型 OTHER {other}，錯誤 {errors - e0}")
    print(f"合計錯誤 {errors} 項")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
