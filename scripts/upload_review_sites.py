#!/usr/bin/env python3
"""把 build_review_site.py 建好的六站題目資料上傳到 Supabase Storage（bucket: review），
供 OLE 題庫網站上的審題台讀取。

    python scripts/upload_review_sites.py <站點輸出根目錄>   # 底下有 s7-1、s7-2、…、s9-2
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batch_api import _env  # noqa: E402

BUCKET = "review"
SITES = ["7-1", "7-2", "8-1", "8-2", "9-1", "9-2"]


def main() -> int:
    root = Path(sys.argv[1])
    url, key = _env("SUPABASE_URL").rstrip("/"), _env("SUPABASE_SERVICE_ROLE_KEY")
    auth = {"Authorization": f"Bearer {key}", "apikey": key}
    if requests.get(f"{url}/storage/v1/bucket/{BUCKET}", headers=auth, timeout=30).status_code != 200:
        requests.post(f"{url}/storage/v1/bucket", headers=auth, timeout=30,
                      json={"id": BUCKET, "name": BUCKET, "public": False}).raise_for_status()
    jobs = []
    for site in SITES:
        base = root / f"s{site}"
        for f in [base / "codes.json", *sorted((base / "data").rglob("*.json"))]:
            jobs.append((f"{site}/{f.relative_to(base).as_posix()}", f))
    session = requests.Session()

    def put(job):
        key_, f = job
        for _ in range(5):
            r = session.post(f"{url}/storage/v1/object/{BUCKET}/{key_}", data=f.read_bytes(), timeout=300,
                             headers={**auth, "Content-Type": "application/json", "x-upsert": "true"})
            if r.status_code in (200, 201):
                return None
        return f"{key_}: {r.status_code} {r.text[:100]}"

    with ThreadPoolExecutor(8) as pool:
        errs = [e for e in pool.map(put, jobs) if e]
    print(f"上傳 {len(jobs) - len(errs)}／{len(jobs)} 個檔案", *errs[:5], sep="\n")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
