"""附圖在 Supabase Storage 的物件鍵。

Supabase 的物件鍵只接受 ASCII，附圖路徑卻含中文（doc_111_台北_新興_…/1_3_opt丁.png），
所以用路徑的 SHA-1 當鍵：同一張圖永遠對到同一個鍵，上傳端與網站端各自算得出來，
不需要另存對照表。
"""

from __future__ import annotations

import hashlib

BUCKET = "assets"


def storage_key(rel_path: str) -> str:
    """data/assets 底下的相對路徑 → 物件鍵（例：3f/3fa9…c1.png）。"""
    h = hashlib.sha1(rel_path.replace("\\", "/").encode("utf-8")).hexdigest()
    return f"{h[:2]}/{h[2:26]}.png"


# ── 讀取附圖：本機有就用本機，否則從 Supabase Storage 下載並快取 ──
import os  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402

ASSET_ROOT = Path(os.environ.get("OLEQS_ASSETS", "data/assets")).resolve()
CACHE = Path(os.environ.get("OLEQS_ASSET_CACHE", "/tmp/oleqs-assets"))
_lock = threading.Lock()


def asset_path(rel: str) -> Path | None:
    """附圖的本機檔案路徑；本機沒有且設了 SUPABASE_URL 時從 Storage 取回。找不到回 None。"""
    rel = rel.replace("\\", "/").lstrip("/")
    local = (ASSET_ROOT / rel).resolve()
    if ASSET_ROOT in local.parents and local.is_file():
        return local
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        return None
    cached = CACHE / storage_key(rel)
    if cached.is_file():
        return cached
    import requests
    r = requests.get(f"{url.rstrip('/')}/storage/v1/object/authenticated/{BUCKET}/{storage_key(rel)}",
                     headers={"Authorization": f"Bearer {key}", "apikey": key}, timeout=60)
    if r.status_code != 200:
        return None
    with _lock:
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(".part")
        tmp.write_bytes(r.content)
        tmp.replace(cached)
    return cached
