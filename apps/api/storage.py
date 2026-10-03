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
