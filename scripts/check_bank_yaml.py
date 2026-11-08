#!/usr/bin/env python3
"""檢查外部轉出的題庫 YAML 是否符合 docs/QUESTION_FORMAT.md，交件前自我檢查用。

    python scripts/check_bank_yaml.py <bank 資料夾或單一 YAML> [--assets <附圖根目錄>] [--existing]

國中、高中卷都適用（高中卷依 document.level: senior 判斷，見 docs/HIGH_SCHOOL_HANDOFF.md）。
會列出：缺必填欄位、卷 id／題目 id 不合規則、題型或大題不對、圖檔不存在、會被品管閘門剔除的題目
（規則同 apps/api/quality.py）。--existing 再比對 data/export/papers.jsonl.gz，列出題庫已有的卷。
只需要 pyyaml；有錯誤時結束碼為 1。
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.quality import evaluate  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
REQUIRED = ("id", "grade", "subject", "academic_year_roc", "semester", "exam_seq", "city", "school_short",
            "school", "title", "exam_name", "sections")
SUBJECTS = {"國文", "英語", "數學", "自然", "社會"}
SUBS = {"自然": {"理化", "生物", "地科"}, "社會": {"歷史", "地理", "公民"}}
# 高中（docs/HIGH_SCHOOL_HANDOFF.md）：自然分物理、化學、生物、地科，社會的公民是「公民與社會」
SUBS_SENIOR = {"自然": {"物理", "化學", "生物", "地科"}, "社會": {"歷史", "地理", "公民與社會"}}
TYPES = {"single", "multiple", "tf", "fill", "calc", "essay", "matching"}
STATUS = {"verified", "ai_generated", "disputed", "missing"}


def doc_id(m: dict) -> str:
    subj = m["subject"] + (f"_{m['sub_subject']}" if m.get("sub_subject") else "")
    if m.get("course"):                              # 高中：數A／數B、選修課程
        subj += f"_{m['course']}"
    return (f"doc_{m['academic_year_roc']}_{m['city']}_{m['school_short']}_{subj}"
            f"_g{m['grade']}s{m['semester']}e{m['exam_seq']}")


def check(path: Path, assets: Path | None) -> tuple[list[str], list[str], int, int]:
    """回傳 (錯誤, 提醒, 題數, 會被剔除的題數)。"""
    err, warn = [], []
    try:
        d = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [f"YAML 讀不進來：{exc}"], [], 0, 0
    m = d.get("document") or {}
    miss = [k for k in REQUIRED if m.get(k) in (None, "", [])]
    if miss:
        err.append(f"document 缺少 {miss}")
    if m.get("subject") and m["subject"] not in SUBJECTS:
        err.append(f"subject 只能是 {sorted(SUBJECTS)}，寫的是「{m['subject']}」")
    senior = m.get("level") == "senior" or (m.get("grade") or 0) >= 10
    subs = SUBS_SENIOR if senior else SUBS
    if m.get("sub_subject") and m.get("sub_subject") not in subs.get(m.get("subject"), set()):
        err.append(f"sub_subject「{m['sub_subject']}」不屬於 {m.get('subject')}")
    if m.get("grade") not in ((10, 11, 12) if senior else (7, 8, 9)):
        err.append(f"grade 應為 {'10、11、12' if senior else '7、8、9'}，寫的是 {m.get('grade')}")
    if senior and m.get("level") != "senior":
        err.append("高中卷 document 要加 level: senior")
    if m.get("semester") not in (1, 2) or m.get("exam_seq") not in (1, 2, 3):
        err.append(f"semester 應為 1、2，exam_seq 應為 1～3（{m.get('semester')}, {m.get('exam_seq')}）")
    if "臺" in str(m.get("city", "")):
        err.append("city 請用「台」不用「臺」")
    if not miss:
        want = doc_id(m)
        if m["id"] != want:
            err.append(f"卷 id 應為 {want}，寫的是 {m['id']}")
        if path.stem != m["id"]:
            err.append(f"檔名應為 {m['id']}.yaml")
    secs = {s.get("ord") for s in m.get("sections") or []}
    shared = {a.get("key"): a for a in d.get("shared_assets") or []}
    qs = d.get("questions") or []
    if not qs:
        err.append("沒有任何題目")
    seen, rejected = set(), 0
    for q in qs:
        qid = q.get("id", "?")
        if q.get("id") in seen:
            err.append(f"{qid}：題目 id 重複")
        seen.add(q.get("id"))
        if m.get("id") and q.get("id") != f"{m['id']}_s{q.get('section')}_{q.get('number')}":
            err.append(f"{qid}：題目 id 應為 {m['id']}_s{q.get('section')}_{q.get('number')}")
        if q.get("section") not in secs:
            err.append(f"{qid}：section {q.get('section')} 不在 sections 裡")
        if q.get("type") not in TYPES:
            err.append(f"{qid}：type「{q.get('type')}」不是 {sorted(TYPES)} 之一")
        if not isinstance(q.get("page"), int):
            warn.append(f"{qid}：缺 page（原卷第幾頁）")
        if q.get("answer") is not None and not isinstance(q["answer"], list):
            err.append(f"{qid}：answer 要寫成清單，例如 [A]")
        if q.get("answer") and q.get("answer_status") not in STATUS:
            warn.append(f"{qid}：有答案時請填 answer_status（verified／ai_generated／disputed）")
        if q.get("shared_asset") and q["shared_asset"] in shared and \
                q["id"] not in (shared[q["shared_asset"]].get("used_by") or []):
            warn.append(f"{qid}：共用圖 {q['shared_asset']} 的 used_by 沒列這題")
        if re.search(r"<(?!/?u[ >])[a-zA-Z/]", (q.get("stem") or "") + (q.get("group_stem") or "")):
            warn.append(f"{qid}：文字裡有 <u> 以外的角括號標籤，網站會當成文字顯示")
        files = [a.get("file") for a in q.get("assets") or []]
        files += [(o.get("asset") or {}).get("file") for o in q.get("options") or []]
        if q.get("shared_asset") in shared:
            files.append(shared[q["shared_asset"]].get("file"))
        if assets:
            for f in files:
                if f and not (assets / f).is_file():
                    err.append(f"{qid}：附圖檔不存在 {f}")
        ok, why = evaluate(q, d)
        if not ok:
            rejected += 1
            warn.append(f"{qid}：會被品管剔除（{'；'.join(why)}）")
    return err, warn, len(qs), rejected


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", type=Path)
    ap.add_argument("--assets", type=Path, help="附圖根目錄（YAML 裡的 file 相對於這裡）")
    ap.add_argument("--existing", action="store_true", help="比對題庫已有的卷（data/export/papers.jsonl.gz）")
    args = ap.parse_args()
    paths = sorted(args.target.glob("*.yaml")) if args.target.is_dir() else [args.target]
    have = set()
    if args.existing:
        with gzip.open(REPO / "data/export/papers.jsonl.gz", "rt", encoding="utf-8") as f:
            have = {json.loads(line)["id"] for line in f}
    n_err = n_q = n_rej = 0
    for p in paths:
        err, warn, nq, rej = check(p, args.assets)
        n_q += nq
        n_rej += rej
        if args.existing and p.stem in have:
            err.append("題庫已有這份卷（同一場考試），不要重複交")
        if err or warn:
            print(f"== {p.name}")
            for e in err:
                print(f"  ✗ {e}")
            for w in warn[:30]:
                print(f"  △ {w}")
            if len(warn) > 30:
                print(f"  △ …另有 {len(warn) - 30} 則提醒")
        n_err += len(err)
    print(f"\n{len(paths)} 份卷、{n_q} 題：錯誤 {n_err} 項；會被品管剔除 {n_rej} 題")
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
