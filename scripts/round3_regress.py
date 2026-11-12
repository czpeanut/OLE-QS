#!/usr/bin/env python3
"""高中管線回歸測試：用督察的 28 份基準卷跑 word_pipeline.py，題數、剔除數、答案數要和基準一模一樣。

    python3 scripts/round3_regress.py --root <校名來源題庫集中庫 本機資料夾> --out out/regress [--ids doc_…,doc_…]
    python3 scripts/round3_regress.py --list            # 列出 28 份基準原卷的雲端檔案 ID 與該放的路徑

基準在 data/high_school/regression_expect.json（裁示 #009、#010）。用途：
  - 新環境第一次跑：證明這個環境排出來的結果和督察一樣；
  - 督察改管線後：證明沒有把以前對的卷改壞。
原始 Word 的 SHA-256 對不上（拿錯檔）、或任何一項數字不同，結束碼 1，把差異印出來回報督察，不要自己改程式。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path)
    ap.add_argument("--list", action="store_true", help="只列出基準原卷的雲端檔案 ID 與 rel_path，不跑")
    ap.add_argument("--out", type=Path, default=Path("out/regress"))
    ap.add_argument("--ids", help="只測這幾份（逗號分隔）；預設全部 28 份")
    a = ap.parse_args()
    base = json.loads((REPO / "data/high_school/regression_expect.json").read_text(encoding="utf-8"))["papers"]
    tasks = {t["id"]: t for t in json.loads((REPO / "data/high_school/round3_tasks.json").read_text(encoding="utf-8"))["papers"]}
    ids = a.ids.split(",") if a.ids else list(base)
    if a.list:
        print("drive_file_id\trel_path（下載後放在 --root 底下這個路徑；檔案知道連結即可讀，雲端搜尋不一定搜得到）")
        for i in ids:
            print(f"{base[i]['drive_file_id']}\t{base[i]['rel_path']}")
        return 0
    if a.root is None:
        ap.error("要指定 --root（或用 --list 列出要下載的原卷）")
    bad = []
    for i in ids:
        src = a.root / tasks[i]["rel_path"]
        if not src.is_file():
            bad.append(f"{i}：找不到原始檔 {tasks[i]['rel_path']}（雲端檔案 ID {base[i].get('drive_file_id')}）")
        elif hashlib.sha256(src.read_bytes()).hexdigest() != base[i]["sha256"]:
            bad.append(f"{i}：原始 Word 檔和基準不是同一個檔（SHA-256 不同），請確認集中庫的檔案")
    if bad:
        print("\n".join(bad))
        return 1
    r = subprocess.run([sys.executable, str(REPO / "scripts/word_pipeline.py"), "--root", str(a.root),
                        "--out", str(a.out), "--ids", ",".join(ids)])
    if r.returncode:
        print(f"管線結束碼 {r.returncode}")
        return 1
    m = json.loads((a.out / "manifest.json").read_text(encoding="utf-8"))
    got = {p["id"]: p for p in m["papers"]}
    deferred = []
    for s in m["skipped"]:
        if s["id"] in ids and s.get("deferred"):
            deferred.append(s["id"])                       # 這個環境沒有 MathType 工具：不算失敗，但也沒驗到
        elif s["id"] in ids:
            bad.append(f"{s['id']}：不交（{s['reason'][:120]}），基準是交件")
    if deferred:
        print(f"留待完整環境、這次沒驗到 {len(deferred)} 份：" + "、".join(deferred))
        ids = [i for i in ids if i not in deferred]
    for i in ids:
        if i not in got:
            continue
        p, e = got[i], base[i]
        now = {"questions": p["questions"], "dropped": len(p["dropped"]), "answered": p.get("answered"), "pages": p["pages"]}
        diff = [f"{k} {e[k]}→{now[k]}" for k in now if now[k] != e[k]]
        if diff:
            bad.append(f"{i}：" + "，".join(diff))
    print(f"\n環境：{m.get('environment')}")
    if bad:
        print(f"和基準不同 {len(bad)} 份：\n  " + "\n  ".join(bad))
        return 1
    print(f"{len(ids)} 份全部和督察基準相同（題數、剔除數、答案數、PDF 頁數）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
