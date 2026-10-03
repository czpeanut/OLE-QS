#!/usr/bin/env python3
"""找回每張附圖在原卷 PDF 上的裁切位置，寫進 asset 的 crop 欄位。

早期擷取時只存了裁好的 PNG，沒記錄位置；審題網頁的「調整邊緣」需要它才能
從 PDF 重新裁切。裁圖是同一頁、同一解析度（CROP_DPI）渲染出來的，所以把頁面
以相同解析度渲染後做模板比對，就能找回像素級準確的位置。

    asset["crop"] = {"page": 頁碼, "rect": [x0, y0, x1, y1]}   # PDF 點座標

已有 crop 的圖略過；比對不夠吻合（規則式舊卷的圖來源不同）的也略過。

用法:
    python scripts/recover_crops.py data/bank --assets data/assets --pdf-root <考卷根目錄>
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import cv2
import fitz
import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vlm_extract import CROP_DPI  # noqa: E402

SCALE = CROP_DPI / 72
MATCH_MAX = 0.03          # 正規化平方差上限：同一次渲染幾乎完全相同


def assets_of(d: dict):
    """逐一產生 (asset dict, 建議頁碼)。"""
    for q in d.get("questions") or []:
        for a in q.get("assets") or []:
            yield a, q.get("page")
        for o in q.get("options") or []:
            if o.get("asset"):
                yield o["asset"], q.get("page")
    for a in d.get("shared_assets") or []:
        yield a, None


def render(doc, pno: int, cache: dict):
    if pno not in cache:
        pix = doc[pno - 1].get_pixmap(dpi=CROP_DPI, colorspace=fitz.csGRAY)
        full = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        small = cv2.resize(full, (pix.width // COARSE, pix.height // COARSE), interpolation=cv2.INTER_AREA)
        cache[pno] = (full, small)
    return cache[pno]


COARSE = 4               # 先在縮小 4 倍的影像上粗找，再在原解析度的小範圍內精修


def locate(page_img, page_small, tmpl) -> tuple[float, int, int] | None:
    th, tw = tmpl.shape
    ph, pw = page_img.shape
    if th > ph or tw > pw:
        return None
    small = cv2.resize(tmpl, (max(1, tw // COARSE), max(1, th // COARSE)), interpolation=cv2.INTER_AREA)
    if min(small.shape) >= 4 and small.shape[0] <= page_small.shape[0] and small.shape[1] <= page_small.shape[1]:
        res = cv2.matchTemplate(page_small, small, cv2.TM_SQDIFF_NORMED)
        _, _, (cx, cy), _ = cv2.minMaxLoc(res)
        m = COARSE * 3
        x0, y0 = max(0, cx * COARSE - m), max(0, cy * COARSE - m)
        x1, y1 = min(pw, cx * COARSE + tw + m), min(ph, cy * COARSE + th + m)
        win = page_img[y0:y1, x0:x1]
        if win.shape[0] >= th and win.shape[1] >= tw:
            res = cv2.matchTemplate(win, tmpl, cv2.TM_SQDIFF_NORMED)
            v, _, (x, y), _ = cv2.minMaxLoc(res)
            if v < MATCH_MAX:
                return v, x0 + x, y0 + y
    res = cv2.matchTemplate(page_img, tmpl, cv2.TM_SQDIFF_NORMED)      # 粗找失準時退回全頁比對
    v, _, (x, y), _ = cv2.minMaxLoc(res)
    return v, x, y


def process(path: str, assets_dir: str, pdf_root: str) -> tuple[str, int, int, int]:
    p = Path(path)
    d = yaml.safe_load(p.read_text(encoding="utf-8"))
    m = d["document"]
    if not str(m.get("extractor", "")).startswith("vlm") or not m.get("source_file"):
        return p.name, 0, 0, 0
    todo = [(a, pg) for a, pg in assets_of(d) if a.get("file") and not a.get("crop")]
    if not todo:
        return p.name, 0, 0, 0
    pdf = Path(pdf_root) / m["source_file"]
    if not pdf.is_file():
        return p.name, 0, 0, len(todo)
    doc = fitz.open(pdf)
    cache: dict = {}
    found = miss = 0
    for a, pg in todo:
        f = Path(assets_dir) / a["file"]
        tmpl = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE) if f.is_file() else None
        if tmpl is None:
            miss += 1
            continue
        pages = ([pg] if pg and 1 <= pg <= len(doc) else []) + \
                [i for i in range(1, len(doc) + 1) if i != pg]
        best = None
        for pno in pages:
            r = locate(*render(doc, pno, cache), tmpl)
            if r and (best is None or r[0] < best[0]):
                best = (r[0], pno, r[1], r[2])
            if best and best[0] < MATCH_MAX:
                break
        if best and best[0] < MATCH_MAX:
            v, pno, x, y = best
            h, w = tmpl.shape
            a["crop"] = {"page": pno, "rect": [round(x / SCALE, 2), round(y / SCALE, 2),
                                               round((x + w) / SCALE, 2), round((y + h) / SCALE, 2)]}
            found += 1
        else:
            miss += 1
    doc.close()
    if found:
        p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p.name, found, miss, 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bank", type=Path)
    ap.add_argument("--assets", type=Path, default=Path("data/assets"))
    ap.add_argument("--pdf-root", type=Path, required=True)
    ap.add_argument("--only")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    paths = [str(p) for p in sorted(args.bank.glob("*.yaml")) if not args.only or args.only in p.name]
    tot_f = tot_m = tot_np = 0
    with ProcessPoolExecutor(args.workers) as pool:
        futs = [pool.submit(process, p, str(args.assets), str(args.pdf_root)) for p in paths]
        for i, fut in enumerate(as_completed(futs), 1):
            name, f, mi, nopdf = fut.result()
            tot_f += f
            tot_m += mi
            tot_np += nopdf
            if i % 200 == 0:
                print(f"[{i}/{len(paths)}] 找到 {tot_f}、找不到 {tot_m}、缺 PDF {tot_np}", flush=True)
    print(f"完成：找到 {tot_f} 張、找不到 {tot_m} 張、缺 PDF {tot_np} 張")
    return 0


if __name__ == "__main__":
    sys.exit(main())
