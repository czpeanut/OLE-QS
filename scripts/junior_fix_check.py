#!/usr/bin/env python3
"""國中「題目有問題」修正交件檢查（給執行 agent；只用標準函式庫）。

  python junior_fix_check.py tasks/修正_數學_1.jsonl REVISED_修正_數學_1.jsonl
有錯誤時結束碼為 1。
"""
import json, re, sys

TYPES = {"single", "multiple", "fill", "calc", "tf", "matching", "essay"}
FIG = re.compile(r"如圖|下圖|附圖|上圖|圖\s*[（(]?[一二三四五六七八九十\d]+[）)]?\s*所示|右圖|左圖")


def load(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    tasks = {t["id"]: t for t in load(sys.argv[1])}
    rev = load(sys.argv[2])
    err, warn, seen = [], [], set()
    for r in rev:
        i = r.get("id")
        if i in seen:
            err.append(f"{i}: 重複")
        seen.add(i)
        t = tasks.get(i)
        if not t:
            err.append(f"{i}: 不在任務檔")
            continue
        res = r.get("result")
        if res == "unfixable":
            if not r.get("unfixable_reason"):
                err.append(f"{i}: unfixable 要寫 unfixable_reason")
            continue
        if res != "fixed":
            err.append(f"{i}: result 只能是 fixed／unfixable")
            continue
        ty, stem = r.get("type"), (r.get("stem") or "").strip()
        opts = r.get("options") or []
        ans = r.get("answer")
        ans = ans if isinstance(ans, list) else ([] if ans in (None, "") else [ans])
        if ty not in TYPES:
            err.append(f"{i}: type 不合法 {ty}")
        if not stem:
            err.append(f"{i}: stem 空白")
        if not r.get("fix_note"):
            err.append(f"{i}: 缺 fix_note")
        if not ans:
            err.append(f"{i}: 缺 answer")
        for f in ("stem", "group_stem"):
            if (r.get(f) or "").count("$") % 2:
                err.append(f"{i}: {f} 的 $ 沒成對")
        if ty in ("single", "multiple"):
            labels = [o.get("label") for o in opts]
            if len(opts) < 2:
                err.append(f"{i}: 選擇題至少 2 個選項")
            elif labels != [chr(65 + k) for k in range(len(opts))]:
                err.append(f"{i}: 選項代號要 A、B、C… 連續：{labels}")
            if any(a not in labels for a in ans):
                err.append(f"{i}: 答案 {ans} 不在選項內")
            if ty == "single" and len(ans) != 1:
                err.append(f"{i}: 單選只能一個答案")
            if any(not (o.get("content") or "").strip() for o in opts) and not any(o.get("image") for o in t["options"]):
                err.append(f"{i}: 有空白選項")
            if t["options"] and any(o.get("image") for o in t["options"]) and labels != [o["label"] for o in t["options"]]:
                err.append(f"{i}: 原選項有附圖，選項代號與數量不可改")
        if not t["figures"] and not any(o.get("image") for o in t["options"]) and FIG.search(stem + (r.get("group_stem") or "")):
            err.append(f"{i}: 沒有圖檔，題幹卻仍依賴圖（如圖／下圖…）")
        if stem == (t["stem"] or "").strip() and opts == [{"label": o["label"], "content": o["content"]} for o in t["options"]] and ans == (t["answer"] or []):
            err.append(f"{i}: 內容和原題完全相同")
    for i in tasks:
        if i not in seen:
            err.append(f"{i}: 沒有交（每題都要一行）")
    n_fix = sum(1 for r in rev if r.get("result") == "fixed")
    print(f"題數 {len(tasks)}，交件 {len(rev)}（fixed {n_fix}、unfixable {len(rev) - n_fix}）；錯誤 {len(err)}")
    for e in err[:50]:
        print("  ✗", e)
    if len(err) > 50:
        print(f"  …另有 {len(err) - 50} 項")
    return 1 if err else 0


if __name__ == "__main__":
    sys.exit(main())
