#!/usr/bin/env python3
"""結構驗證器：在人工校對之前，先用純規則抓出擷取管線的錯誤。

這些檢查不需要知道正確答案，只靠考卷自身的內部一致性就能發現問題，
成本近乎為零，卻能攔下大部分「整題遺漏」「題號跳號」「選項缺一個」的事故。

用法:
    python scripts/validate_extraction.py data/samples/expected/*.yaml
    python scripts/validate_extraction.py out/extracted.json
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import yaml

# 各題型預期的選項數；None 表示不檢查
EXPECTED_OPTION_COUNT = {"single": 4, "multiple": None, "tf": None}
# 不應該有選項的題型
NO_OPTION_TYPES = {"fill", "calc", "essay", "group"}


def load(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yaml", ".yml"}:
        return yaml.safe_load(text)
    return json.loads(text)


def check(doc: dict) -> list[tuple[str, str]]:
    """回傳 [(severity, message)]，severity 為 ERROR / WARN。"""
    issues: list[tuple[str, str]] = []
    err = lambda m: issues.append(("ERROR", m))
    warn = lambda m: issues.append(("WARN", m))

    meta = doc.get("document", {})
    questions = doc.get("questions", [])

    if not questions:
        err("沒有擷取到任何題目")
        return issues

    # ── 1. 配分總和 ──────────────────────────────────────────────
    # 考卷幾乎都會在標題列宣告總分與各大題配分，這是最強的一道防線：
    # 少擷取一題、多擷取一題，總分立刻對不上。
    declared_total = meta.get("total_score")
    computed_total = sum(q.get("score") or 0 for q in questions)
    if declared_total is not None and computed_total != declared_total:
        err(f"配分總和不符：擷取得 {computed_total} 分，考卷宣告 {declared_total} 分")

    # ── 2. 各大題題數與配分 ───────────────────────────────────────
    by_section = Counter(q.get("section") for q in questions)
    for sec in meta.get("sections", []):
        ord_, name = sec.get("ord"), sec.get("name")
        actual = by_section.get(ord_, 0)
        if sec.get("count") is not None and actual != sec["count"]:
            err(f"{name}：宣告 {sec['count']} 題，實際擷取 {actual} 題")

        per = sec.get("per_item_score")
        if per is not None:
            bad = [q["id"] for q in questions
                   if q.get("section") == ord_ and q.get("score") != per]
            if bad:
                warn(f"{name}：每題應為 {per} 分，下列題目配分不符 {bad}")

    # ── 3. 題號連續性（分大題各自從 1 開始） ────────────────────────
    # 注意：台灣段考卷的題號在每個大題會重新編號，不能用全卷流水號檢查。
    for sec_ord in sorted(by_section):
        nums = [q.get("number") for q in questions if q.get("section") == sec_ord]
        nums = [n for n in nums if isinstance(n, int)]
        expected = list(range(1, len(nums) + 1))
        if sorted(nums) != expected:
            missing = set(expected) - set(nums)
            dupes = [n for n, c in Counter(nums).items() if c > 1]
            detail = []
            if missing:
                detail.append(f"缺 {sorted(missing)}")
            if dupes:
                detail.append(f"重複 {sorted(dupes)}")
            err(f"第 {sec_ord} 大題題號不連續：{'；'.join(detail) or nums}")

    # ── 4. 題目本身的完整性 ──────────────────────────────────────
    seen_ids: set[str] = set()
    for q in questions:
        qid = q.get("id", "<無 id>")
        if qid in seen_ids:
            err(f"{qid}：id 重複")
        seen_ids.add(qid)

        if not (q.get("stem") or "").strip():
            err(f"{qid}：題幹為空")

        qtype = q.get("type")
        opts = q.get("options") or []

        if qtype in NO_OPTION_TYPES and opts:
            warn(f"{qid}：{qtype} 題型不應有選項，卻擷取到 {len(opts)} 個")

        expected_opts = EXPECTED_OPTION_COUNT.get(qtype)
        if expected_opts is not None and len(opts) != expected_opts:
            err(f"{qid}：{qtype} 題預期 {expected_opts} 個選項，實際 {len(opts)} 個")

        labels = [o.get("label") for o in opts]
        if labels and labels != sorted(labels):
            warn(f"{qid}：選項標籤順序異常 {labels}")
        if len(set(labels)) != len(labels):
            err(f"{qid}：選項標籤重複 {labels}")
        for o in opts:
            if not (o.get("content") or "").strip():
                err(f"{qid} 選項 {o.get('label')}：內容為空")

        # 題組必須有子題
        if qtype == "group" and not q.get("children"):
            err(f"{qid}：題組沒有子題")
        # 子題配分加總應等於題組配分
        if q.get("children"):
            child_sum = sum(c.get("score") or 0 for c in q["children"])
            if q.get("score") and child_sum != q["score"]:
                warn(f"{qid}：子題配分合計 {child_sum} ≠ 題組配分 {q['score']}")

        # 題幹引用的資產必須存在
        asset_keys = {a.get("key") for a in q.get("assets", [])}
        for key in asset_keys:
            if key and not (q.get("assets")):
                err(f"{qid}：資產 {key} 未定義")
        for a in q.get("assets", []):
            if a.get("kind") == "figure" and not (a.get("url") or a.get("must_crop")):
                warn(f"{qid} 資產 {a.get('key')}：圖形資產尚未產生檔案")
            if a.get("kind") == "table" and not (a.get("markdown") or a.get("url")):
                err(f"{qid} 資產 {a.get('key')}：表格既無結構化內容也無圖檔")

        # LaTeX 錢字號需成對
        for field, text in [("題幹", q.get("stem") or "")] + \
                           [(f"選項{o.get('label')}", o.get("content") or "") for o in opts]:
            if text.count("$") % 2 != 0:
                err(f"{qid} {field}：LaTeX 的 $ 數量為奇數，公式未閉合")

    # ── 5. 答案掛載 ─────────────────────────────────────────────
    if meta.get("answer_key_available"):
        no_answer = [q["id"] for q in questions
                     if q.get("type") != "group" and not q.get("answer")]
        if no_answer:
            err(f"聲明有答案卷，但下列題目未掛上答案：{no_answer}")

    return issues


def main(argv: list[str]) -> int:
    paths = [Path(p) for p in argv[1:]]
    if not paths:
        print(__doc__)
        return 2

    exit_code = 0
    for path in paths:
        doc = load(path)
        issues = check(doc)
        errors = [m for sev, m in issues if sev == "ERROR"]
        warns = [m for sev, m in issues if sev == "WARN"]

        n = len(doc.get("questions", []))
        status = "FAIL" if errors else ("WARN" if warns else "PASS")
        print(f"\n{'=' * 60}\n{path}  —  {n} 題  [{status}]\n{'=' * 60}")
        for m in errors:
            print(f"  ERROR  {m}")
        for m in warns:
            print(f"  WARN   {m}")
        if not issues:
            print("  所有結構檢查通過")
        if errors:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
