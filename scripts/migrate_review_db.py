#!/usr/bin/env python3
"""把 claude.ai 審題台資料庫匯出的紀錄（ArtifactData list 存成的 JSON 檔）寫進 Supabase 的 review_doc。

    python scripts/migrate_review_db.py <匯出根目錄> [--names id=名字,...]

匯出根目錄底下是 <站>/<集合>/<文件ID>.json（文件 ID 裡的「~」在檔名中是「@」）。
重跑會覆蓋同 ID 的紀錄，不會重複。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batch_api import _env  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, nargs="+")
    ap.add_argument("--names", default="")
    args = ap.parse_args()
    url, key = _env("SUPABASE_URL").rstrip("/"), _env("SUPABASE_SERVICE_ROLE_KEY")
    H = {"Authorization": f"Bearer {key}", "apikey": key, "Content-Type": "application/json",
         "Prefer": "resolution=merge-duplicates,return=minimal"}
    rows = []
    for root in args.root:
        for f in root.rglob("*.json"):
            site, col = f.parent.parent.name, f.parent.name
            if col not in ("reports", "reviewed", "crops"):
                continue
            x = json.loads(f.read_text(encoding="utf-8"))
            rows.append({"site": site, "collection": col, "id": f.stem.replace("@", "~"), "data": x.get("data", x)})
    for kv in filter(None, args.names.split(",")):
        i, n = kv.split("=", 1)
        rows.append({"site": "_all", "collection": "people", "id": i, "data": {"name": n}})
    r = requests.post(f"{url}/rest/v1/review_doc", headers=H, data=json.dumps(rows, ensure_ascii=False).encode(), timeout=60)
    print(r.status_code, r.text[:200] or f"寫入 {len(rows)} 筆")
    for row in rows:
        print(" ", row["site"], row["collection"], row["id"])
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
