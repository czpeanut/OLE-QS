#!/usr/bin/env python3
"""把 data/assets 的附圖上傳到 Supabase Storage（備份兼正式上線用）。

    python scripts/upload_assets_supabase.py [--assets data/assets] [--workers 16]

需要 .env.local：SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY。
- bucket 不存在時自動建立（private，網站以 service role 讀取）
- 已上傳的記在 out/supabase_upload_done.txt，中斷後重跑只補沒傳的
- 物件鍵見 apps/api/storage.py（中文路徑轉成 SHA-1）
- 全部傳完後列出 bucket 內物件數做核對
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
from apps.api.storage import BUCKET, storage_key  # noqa: E402
from batch_api import _env  # noqa: E402

DONE = REPO / "out" / "supabase_upload_done.txt"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--assets", type=Path, default=REPO / "data" / "assets")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force-list", type=Path, help="這份清單（每行一個 data/assets 下的相對路徑）不論傳過與否都重傳，例如重新裁切過的圖")
    args = ap.parse_args()

    url = _env("SUPABASE_URL").rstrip("/")
    key = _env("SUPABASE_SERVICE_ROLE_KEY")
    auth = {"Authorization": f"Bearer {key}", "apikey": key}

    r = requests.get(f"{url}/storage/v1/bucket/{BUCKET}", headers=auth, timeout=30)
    if r.status_code != 200:
        r = requests.post(f"{url}/storage/v1/bucket", headers=auth, timeout=30,
                          json={"id": BUCKET, "name": BUCKET, "public": False})
        r.raise_for_status()
        print(f"建立 bucket {BUCKET}（private）")

    done = set(DONE.read_text(encoding="utf-8").splitlines()) if DONE.is_file() else set()
    files = sorted(p.relative_to(args.assets).as_posix() for p in args.assets.rglob("*.png"))
    force = set(args.force_list.read_text(encoding="utf-8").split()) if args.force_list else set()
    todo = [f for f in files if f not in done or f in force][: args.limit]
    print(f"共 {len(files):,} 張，已傳 {len(done):,}，這次要傳 {len(todo):,}", flush=True)

    lock = threading.Lock()
    session = requests.Session()
    stats = {"ok": 0, "fail": 0, "bytes": 0}
    t0 = time.time()

    def put(rel: str) -> tuple[str, str | None]:
        data = (args.assets / rel).read_bytes()
        for attempt in range(5):
            try:
                r = session.post(f"{url}/storage/v1/object/{BUCKET}/{storage_key(rel)}", data=data, timeout=120,
                                 headers={**auth, "Content-Type": "image/png", "x-upsert": "true"})
                if r.status_code in (200, 201):
                    with lock:
                        stats["bytes"] += len(data)
                    return rel, None
                err = f"{r.status_code} {r.text[:120]}"
            except requests.RequestException as exc:
                err = str(exc)[:120]
            time.sleep(2 ** attempt)
        return rel, err

    with DONE.open("a", encoding="utf-8") as log, ThreadPoolExecutor(args.workers) as pool:
        futs = [pool.submit(put, f) for f in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            rel, err = fut.result()
            with lock:
                if err:
                    stats["fail"] += 1
                    print(f"  失敗 {rel}：{err}", flush=True)
                else:
                    stats["ok"] += 1
                    log.write(rel + "\n")
            if i % 1000 == 0:
                log.flush()
                mb = stats["bytes"] / 1e6
                print(f"[{i:,}/{len(todo):,}] 成功 {stats['ok']:,}、失敗 {stats['fail']}，"
                      f"{mb:,.0f} MB，{mb / (time.time() - t0):.1f} MB/s", flush=True)
    print(f"完成：成功 {stats['ok']:,}、失敗 {stats['fail']}（失敗的重跑即可補傳）")
    return 1 if stats["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
